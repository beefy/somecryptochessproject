"""The node: chain, mempool, transcripts and gossip in one object (PROTOCOL.md section 14).

``NodeService`` is the library behind the HTTP API, the CLI and the devnet. It
owns exactly one :class:`~sccp.chain.chain.Chain`, the :class:`~sccp.chain.mempool.Mempool`
re-based on that chain's tip, and an optional :class:`~sccp.chain.storage.Storage`
that every accepted block is written through.

Nothing here opens a socket: the API layer wraps the service in FastAPI, and
:class:`HttpPeerClient` speaks to a peer *through an injected*
``httpx.Client``. That is what lets tests run two nodes in-process and still
exercise the real HTTP routes.
"""

from __future__ import annotations

import os
import random
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import httpx

from sccp.chain.block import block_hash
from sccp.chain.chain import Chain, StoredBlock
from sccp.chain.genesis import load_genesis
from sccp.chain.mempool import Mempool
from sccp.chain.state import ChallengeRecord, ProofRecord
from sccp.chain.storage import Storage, restore_chain
from sccp.chain.tx import txid
from sccp.chess.openings import OpeningBook
from sccp.config import NetworkParams, get_network
from sccp.crypto.address import validate_address
from sccp.errors import (
    AddressError,
    FaucetError,
    GenesisError,
    GossipError,
    LedgerError,
    NotFoundError,
    ProtocolError,
)
from sccp.node.faucet import Faucet
from sccp.node.models import (
    AccountView,
    BlockIngest,
    BlockList,
    BlockSummary,
    BlockView,
    ChallengeList,
    ChallengeView,
    DifficultyPoint,
    DifficultyView,
    EpochView,
    FaucetView,
    GameList,
    GameSummary,
    GameView,
    GossipResult,
    MatchInfo,
    MempoolStatus,
    MempoolView,
    NodeStatus,
    PeerView,
    ProofAccepted,
    SyncReport,
    TransactionRecord,
    TxAccepted,
)
from sccp.node.p2p import (
    MAX_FANOUT,
    GossipCache,
    GossipEnvelope,
    GossipKind,
    PeerClient,
    PeerTable,
    choose_fanout,
    fetch_blocks,
    object_id_of,
    plan_sync,
    verify_envelope,
)
from sccp.protocol.models import Beacon, Block, QualitySummary, Transaction, Transcript, WinProof
from sccp.version import PROTOCOL_VERSION

GENESIS_DIR_ENV = "SCCP_GENESIS_DIR"
"""Environment variable pointing at a directory of ``<network>.json`` files."""


def unix_now() -> int:
    """Wall-clock seconds; injected in tests so nothing sleeps."""
    return int(time.time())


def default_genesis_path(network: str) -> Path:
    """Find ``<network>.json`` in ``$SCCP_GENESIS_DIR``, the package or the repo.

    The search order is deliberate: an explicit environment override wins
    (containers and tests), then the files packaged inside the wheel (so
    ``pip install sccp`` followed by ``sccp node run`` works anywhere), then a
    ``genesis/`` directory next to the source checkout.
    """
    candidates: list[Path] = []
    configured = os.environ.get(GENESIS_DIR_ENV)
    if configured:
        candidates.append(Path(configured))
    candidates.append(Path(__file__).resolve().parents[1] / "chain" / "data" / "genesis")
    candidates.append(Path.cwd() / "genesis")
    candidates.append(Path(__file__).resolve().parents[3] / "genesis")
    for directory in candidates:
        path = directory / f"{network}.json"
        if path.is_file():
            return path
    searched = ", ".join(str(path) for path in candidates)
    raise GenesisError(
        f"no genesis document for {network!r}; set {GENESIS_DIR_ENV} or pass an explicit path "
        f"(searched: {searched})"
    )


def _quality_verdict(summary: QualitySummary | None) -> Literal["pass", "fail"] | None:
    """Narrow a summary verdict to the two values the games API exposes."""
    if summary is None:
        return None
    if summary.verdict == "pass":
        return "pass"
    if summary.verdict == "fail":
        return "fail"
    return None


def _block_summary(stored: StoredBlock) -> BlockSummary:
    """Project one stored block onto its list row."""
    header = stored.header
    return BlockSummary(
        hash=stored.block_hash,
        parent_hash=stored.parent_hash,
        height=stored.height,
        timestamp=header.timestamp,
        epoch=header.epoch,
        miner=header.miner,
        transactions=len(stored.block.transactions),
        proofs=len(stored.block.proofs),
        wins_required=header.wins_required,
        seal_bits=header.seal_bits,
    )


def _proof_in(stored: StoredBlock | None, game_hash: str) -> WinProof | None:
    """The full proof for ``game_hash`` if the node still holds its block."""
    if stored is None:
        return None
    for proof in stored.block.proofs:
        if proof.game_hash == game_hash:
            return proof
    return None


def _pending_summary(proof: WinProof, status: str) -> GameSummary:
    """Project a mempool proof onto its list row."""
    return GameSummary(
        game_hash=proof.game_hash,
        proof_id=proof.proof_id,
        match_id=proof.match_id,
        epoch=proof.epoch,
        round=proof.round,
        winner=proof.winner,
        loser=proof.loser,
        winner_seat=proof.winner_seat,
        result=proof.result,
        termination=proof.termination,
        plies=proof.plies,
        in_mempool=True,
        low_ply=proof.low_ply,
        challenge_status=status,  # type: ignore[arg-type]
        quality_verdict=_quality_verdict(proof.quality_summary),
    )


def _settled_summary(record: ProofRecord, proof: WinProof | None, status: str) -> GameSummary:
    """Project a settled proof onto its list row.

    The ledger keeps only ``(proof_id, height, winner)`` per game, so the result
    fields come from the block body. A node that snapshotted away the body still
    lists the game, it just cannot describe the game.
    """
    if proof is None:
        return GameSummary(
            game_hash=record.game_hash,
            proof_id=record.proof_id,
            winner=record.winner,
            height=record.height,
            challenge_status=status,  # type: ignore[arg-type]
        )
    return GameSummary(
        game_hash=proof.game_hash,
        proof_id=record.proof_id,
        match_id=proof.match_id,
        epoch=proof.epoch,
        round=proof.round,
        winner=proof.winner,
        loser=proof.loser,
        winner_seat=proof.winner_seat,
        result=proof.result,
        termination=proof.termination,
        plies=proof.plies,
        height=record.height,
        low_ply=proof.low_ply,
        challenge_status=status,  # type: ignore[arg-type]
        quality_verdict=_quality_verdict(proof.quality_summary),
    )


@dataclass(slots=True)
class NodeService:
    """One node's ledger, pools, peer table and transcript cache."""

    params: NetworkParams
    chain: Chain
    mempool: Mempool
    storage: Storage | None = None
    clock: Callable[[], int] = unix_now
    transcripts: dict[str, Transcript] = field(default_factory=dict)
    beacons: dict[int, Beacon] = field(default_factory=dict)
    peers: PeerTable = field(default_factory=PeerTable)
    gossip: GossipCache = field(default_factory=GossipCache)
    rng: random.Random = field(default_factory=random.Random)
    faucet: Faucet | None = None
    write_lock: threading.RLock = field(default_factory=threading.RLock, repr=False, compare=False)
    fanout: int = MAX_FANOUT
    max_sync_blocks: int = 512
    syncing: bool = False

    def __post_init__(self) -> None:
        if self.chain.params.network_id != self.params.network_id:
            raise ProtocolError(
                f"chain is on {self.chain.params.network_id!r}, node is {self.params.network_id!r}"
            )
        if (
            self.params.genesis_hash is not None
            and self.chain.genesis_hash != self.params.genesis_hash
        ):
            raise GenesisError(
                f"genesis hash {self.chain.genesis_hash} does not match the pinned "
                f"{self.params.genesis_hash}"
            )
        if self.storage is not None:
            self.storage.bind_network(self.params)
        if self.faucet is None and self.params.network_id in ("testnet", "devnet"):
            self.faucet = Faucet(
                params=self.params,
                state=lambda: self.mempool.projection,
                admit=lambda tx: self.mempool.add_transaction(tx),
                clock=self.clock,
            )

    # -- construction -------------------------------------------------------

    @classmethod
    def open(
        cls,
        network: str | NetworkParams,
        *,
        genesis_path: str | Path | None = None,
        storage: Storage | None = None,
        book: OpeningBook | None = None,
        clock: Callable[[], int] = unix_now,
    ) -> NodeService:
        """Open a node, replaying ``storage`` when one is supplied."""
        params = get_network(network)
        path = (
            Path(genesis_path)
            if genesis_path is not None
            else default_genesis_path(params.network_id)
        )
        genesis_block, genesis_state = load_genesis(path, params)
        if storage is not None:
            chain = restore_chain(params, genesis_block, genesis_state, storage)
        else:
            chain = Chain(params=params, genesis_block=genesis_block, genesis_state=genesis_state)
        chain.book = book
        mempool = Mempool(params=params, tip_state=chain.state, book=book)
        return cls(
            params=params,
            chain=chain,
            mempool=mempool,
            storage=storage,
            clock=clock,
        )

    def check_startup(self) -> None:
        """Fail fast before serving: network id, pinned genesis and storage.

        The reference-engine policy is intentionally not enforced here: the node
        does not run an engine at startup. Quality is handled by the sampler and
        bonded challenges after a proof is accepted.
        """
        if self.chain.params.network_id != self.params.network_id:
            raise ProtocolError(
                f"chain is on {self.chain.params.network_id!r}, node is {self.params.network_id!r}"
            )
        if (
            self.params.genesis_hash is not None
            and self.chain.genesis_hash != self.params.genesis_hash
        ):
            raise GenesisError(
                f"genesis hash {self.chain.genesis_hash} does not match the pinned "
                f"{self.params.genesis_hash}"
            )
        if self.storage is not None:
            self.storage.bind_network(self.params)

    def close(self) -> None:
        """Release the storage handle on shutdown."""
        if self.storage is not None:
            self.storage.close()

    # -- reads --------------------------------------------------------------

    @property
    def height(self) -> int:
        """The height of the current tip."""
        return self.chain.height

    @property
    def tip_hash(self) -> str:
        """The hash of the current tip."""
        return self.chain.tip_hash

    @property
    def genesis_hash(self) -> str:
        """The hash of the genesis block."""
        return self.chain.genesis_hash

    def status(self) -> NodeStatus:
        """Height, tip, peers and pool sizes."""
        return NodeStatus(
            network=self.params.network_id,
            protocol_version=PROTOCOL_VERSION,
            height=self.height,
            tip_hash=self.tip_hash,
            genesis_hash=self.genesis_hash,
            peers=len(self.peers),
            syncing=self.syncing,
            mempool=MempoolStatus(
                transactions=len(self.mempool.transactions),
                proofs=len(self.mempool.proofs),
            ),
        )

    def block(self, reference: str) -> BlockView:
        """Look a block up by height (decimal) or by 64-hex hash."""
        if reference.isdigit():
            stored = self.chain.block_at_height(int(reference))
        else:
            stored = self.chain.get(reference.lower())
        if stored is None:
            raise NotFoundError(f"unknown block {reference!r}")
        return BlockView(
            network=self.params.network_id,
            hash=stored.block_hash,
            parent_hash=stored.parent_hash,
            height=stored.height,
            chain_work=stored.chain_work,
            main_chain=self.chain.is_main_chain(stored.block_hash),
            block=stored.block,
        )

    def blocks(self, *, limit: int = 25, before_height: int | None = None) -> BlockList:
        """The newest ``limit`` blocks on the adopted branch, tip first.

        ``before_height`` is the page cursor: pass the lowest height you already
        have to continue backwards. The list is a projection of the header, so it
        is cheap to poll; a block's body is one ``GET /v1/blocks/{height}`` away.
        """
        main = self.chain.main_chain()
        if before_height is not None:
            main = main[:before_height]
        window = main[-limit:] if limit < len(main) else main
        return BlockList(
            network=self.params.network_id,
            tip_height=self.height,
            blocks=tuple(_block_summary(stored) for stored in reversed(window)),
        )

    def account(self, address: str) -> AccountView:
        """Balance, bond, nonce and registration for ``address``."""
        if not validate_address(address, self.params):
            raise AddressError(f"{address!r} is not a {self.params.network_id} address")
        account = self.chain.state.account(address)
        return AccountView(
            network=self.params.network_id,
            address=address,
            balance=account.balance,
            immature=account.immature,
            bonded=account.bonded,
            total=account.total,
            nonce=account.nonce,
            registered_until_epoch=account.registered_until_epoch,
            endpoint=account.endpoint,
            engine=account.engine,
            public_key=account.public_key,
            banned_until=account.banned_until,
        )

    def epoch(self, epoch: int) -> EpochView:
        """The beacon, roster and reveals the chain recorded for ``epoch``.

        A beacon seeded into :attr:`beacons` (by a relay config or a test) is
        served even before the chain opens the epoch, which is what a node that
        follows an external beacon source needs.
        """
        record = self.chain.state.epochs.get(epoch)
        seeded = self.beacons.get(epoch)
        if record is None and seeded is None:
            raise NotFoundError(f"epoch {epoch} has not been opened by this chain")
        beacon = record.beacon if record is not None and record.beacon is not None else seeded
        return EpochView(
            epoch=epoch,
            network=self.params.network_id,
            beacon=beacon,
            roster_root=record.roster_root if record is not None else "",
            roster=dict(record.roster) if record is not None else {},
            availability=dict(record.availability) if record is not None else {},
            quotas=dict(record.quotas) if record is not None else {},
            matchable=record.matchable if record is not None else False,
            ended_at_height=record.ended_at_height if record is not None else None,
        )

    def beacon(self, epoch: int) -> Beacon:
        """Just the beacon for ``epoch`` (404 when the chain has none)."""
        view = self.epoch(epoch)
        if view.beacon is None:
            raise NotFoundError(f"epoch {epoch} has no beacon")
        return view.beacon

    def difficulty(self) -> DifficultyView:
        """The tip's target plus the retarget boundary history."""
        current = self.chain.state.difficulty
        wins = current.wins_required if current else self.params.wins_required
        seal = current.seal_bits if current else self.params.seal_bits
        interval = self.params.retarget_interval
        history: list[DifficultyPoint] = []
        for stored in self.chain.main_chain():
            header = stored.header
            boundary = header.height % interval == 0
            changed = bool(history) and (
                history[-1].wins_required != header.wins_required
                or history[-1].seal_bits != header.seal_bits
            )
            if boundary or changed or not history:
                history.append(
                    DifficultyPoint(
                        height=header.height,
                        wins_required=header.wins_required,
                        seal_bits=header.seal_bits,
                    )
                )
        return DifficultyView(
            network=self.params.network_id,
            height=self.height,
            wins_required=wins,
            seal_bits=seal,
            retarget_interval=interval,
            retarget_enabled=self.params.retarget_enabled,
            history=tuple(history[-16:]),
        )

    def mempool_view(self) -> MempoolView:
        """Pending txids and proof ids, never the bodies."""
        return MempoolView(
            network=self.params.network_id,
            transactions=tuple(sorted(self.mempool.transactions)),
            proofs=tuple(sorted(self.mempool.proofs)),
        )

    def game(self, game_hash: str) -> GameView:
        """Proof and challenge status for ``game_hash``."""
        from sccp.protocol.challenges import challenge_status

        status = challenge_status(self.chain.state, game_hash)
        record = self.chain.state.proofs.get(game_hash)
        if record is not None:
            transcript = self._transcript_for_game(game_hash)
            proof = self._proof_for_record(record.height, game_hash)
            summary = proof.quality_summary if proof is not None else None
            return GameView(
                game_hash=game_hash,
                network=self.params.network_id,
                proof_id=record.proof_id,
                match_id=transcript.certificate.match_id if transcript else None,
                winner=record.winner,
                height=record.height,
                challenge_status=status,  # type: ignore[arg-type]
                quality_verdict=_quality_verdict(summary),
                quality_trace_hash=summary.trace_hash if summary is not None else None,
            )
        for proof in self.mempool.proofs.values():
            if proof.game_hash == game_hash:
                summary = proof.quality_summary
                return GameView(
                    game_hash=game_hash,
                    network=self.params.network_id,
                    proof_id=proof.proof_id,
                    match_id=proof.match_id,
                    winner=proof.winner,
                    in_mempool=True,
                    challenge_status=status,  # type: ignore[arg-type]
                    quality_verdict=_quality_verdict(summary),
                    quality_trace_hash=summary.trace_hash if summary is not None else None,
                )
        raise NotFoundError(f"unknown game {game_hash!r}")

    def games(self, *, limit: int = 25, before_height: int | None = None) -> GameList:
        """Recent settled games, newest first, preceded by the proofs in the pool.

        ``before_height`` pages backwards from a settle height; pending proofs
        only appear on the first page because they have no height to page by.
        Each row is described from the proof itself when this node still holds
        the block, which is what lets an explorer show result, termination and
        ply count without a second request.
        """
        from sccp.protocol.challenges import challenge_status

        by_height = {stored.height: stored for stored in self.chain.main_chain()}
        pending: list[GameSummary] = []
        if before_height is None:
            for proof in self.mempool.proofs.values():
                pending.append(
                    _pending_summary(proof, challenge_status(self.chain.state, proof.game_hash))
                )
        settled: list[GameSummary] = []
        for record in sorted(
            self.chain.state.proofs.values(), key=lambda item: (-item.height, item.game_hash)
        ):
            if before_height is not None and record.height >= before_height:
                continue
            if len(settled) >= limit:
                break
            settled.append(
                _settled_summary(
                    record,
                    _proof_in(by_height.get(record.height), record.game_hash),
                    challenge_status(self.chain.state, record.game_hash),
                )
            )
        return GameList(network=self.params.network_id, games=(*pending, *settled))

    def _proof_for_record(self, height: int, game_hash: str) -> WinProof | None:
        """Find the full proof in its block, if this node still has that block."""
        stored = self.chain.block_at_height(height)
        if stored is None:
            return None
        for proof in stored.block.proofs:
            if proof.game_hash == game_hash:
                return proof
        return None

    def challenges(self, *, include_resolved: bool = False) -> ChallengeList:
        """Quality challenges, open ones by default (``GET /v1/challenges``)."""
        records = [
            record
            for record in self.chain.state.challenges.values()
            if include_resolved or record.open
        ]
        records.sort(key=lambda record: (record.opened_height, record.challenge_id))
        return ChallengeList(
            network=self.params.network_id,
            challenges=tuple(self._challenge_view(record) for record in records),
        )

    def challenge(self, challenge_id: str) -> ChallengeView:
        """One challenge by id, resolved or open."""
        record = self.chain.state.challenges.get(challenge_id)
        if record is None:
            raise NotFoundError(f"unknown challenge {challenge_id!r}")
        return self._challenge_view(record)

    def _challenge_view(self, record: ChallengeRecord) -> ChallengeView:
        """Map a :class:`ChallengeRecord` to its wire form."""
        return ChallengeView(
            challenge_id=record.challenge_id,
            network=self.params.network_id,
            proof_id=record.proof_id,
            game_hash=record.game_hash,
            challenger=record.challenger,
            miner=record.miner,
            reason=record.reason,
            evidence=record.evidence,
            bond=record.bond,
            opened_height=record.opened_height,
            deadline_height=record.deadline_height,
            status=record.status,  # type: ignore[arg-type]
            resolved_height=record.resolved_height,
            panel=record.panel,
        )

    def match(self, match_id: str) -> MatchInfo:
        """Transcript metadata for ``match_id``, from the chain or the cache."""
        transcript = self.transcripts.get(match_id)
        record = self.chain.state.matches.get(match_id)
        if record is None and transcript is None:
            raise NotFoundError(f"unknown match {match_id!r}")
        certificate = transcript.certificate if transcript is not None else None
        if record is not None:
            epoch, round_number, seats = record.epoch, record.round, record.seats
        else:
            if certificate is None:  # pragma: no cover - guarded above
                raise NotFoundError(f"unknown match {match_id!r}")
            epoch, round_number = certificate.epoch, certificate.round
            seats = (certificate.white, certificate.black)
        return MatchInfo(
            match_id=match_id,
            network=self.params.network_id,
            epoch=epoch,
            round=round_number,
            seats=seats,
            certificate=certificate,
            transcript=transcript,
        )

    def transaction(self, digest: str) -> TransactionRecord:
        """Where a transaction is: in the pool, in a block, or nowhere."""
        entry = self.mempool.transactions.get(digest)
        if entry is not None:
            return TransactionRecord(
                txid=digest,
                network=self.params.network_id,
                type=entry.tx.type,
                in_mempool=True,
            )
        for stored in self.chain.main_chain():
            candidates = [stored.block.coinbase, *stored.block.transactions]
            for item in candidates:
                if item is not None and txid(item) == digest:
                    return TransactionRecord(
                        txid=digest,
                        network=self.params.network_id,
                        type=item.type,
                        height=stored.height,
                        block_hash=stored.block_hash,
                    )
        raise NotFoundError(f"unknown transaction {digest!r}")

    def peers_view(self) -> tuple[PeerView, ...]:
        """Every known peer, sorted by URL."""
        return tuple(
            PeerView(
                network=self.params.network_id,
                url=record.url,
                address=record.address,
                height=record.height,
                failures=record.failures,
                banned_until=record.banned_until,
                last_seen=record.last_seen,
            )
            for _, record in sorted(self.peers.records.items())
        )

    def transcript(self, match_id: str) -> Transcript:
        """The full transcript for ``match_id``, when the node has seen it."""
        try:
            return self.transcripts[match_id]
        except KeyError:
            raise NotFoundError(f"no transcript for match {match_id!r}") from None

    def _transcript_for_game(self, game_hash: str) -> Transcript | None:
        for transcript in self.transcripts.values():
            if transcript.result.game_hash == game_hash:
                return transcript
        return None

    # -- writes -------------------------------------------------------------

    def submit_transaction(self, tx: Transaction) -> TxAccepted:
        """Admit a signed transaction and return its txid."""
        if tx.network != self.params.network_id:
            raise ProtocolError(
                f"transaction is for {tx.network!r}, this node is {self.params.network_id!r}"
            )
        with self.write_lock:
            digest = self.mempool.add_transaction(tx)
        return TxAccepted(network=self.params.network_id, txid=digest)

    def faucet_request(self, address: str, *, client: str, amount: int | None = None) -> FaucetView:
        """Dispense one faucet grant and admit its transfer to the mempool."""
        if self.faucet is None:
            raise FaucetError(f"the faucet is disabled on {self.params.network_id}")
        with self.write_lock:
            tx = self.faucet.request(address, client=client, amount=amount)
        return FaucetView(
            network=self.params.network_id,
            address=address,
            amount=self.faucet.policy.amount_coins if amount is None else amount,
            txid=txid(tx),
        )

    def announce_proof(self, proof: WinProof) -> ProofAccepted:
        """Admit a win proof after the same checks a block would apply."""
        if proof.network != self.params.network_id:
            raise ProtocolError(
                f"proof is for {proof.network!r}, this node is {self.params.network_id!r}"
            )
        with self.write_lock:
            proof_id = self.mempool.add_proof(proof)
        return ProofAccepted(network=self.params.network_id, proof_id=proof_id)

    def add_transcript(self, transcript: Transcript) -> None:
        """Cache a transcript so ``/v1/matches`` can serve it."""
        certificate = transcript.certificate
        if certificate.network != self.params.network_id:
            raise ProtocolError(
                f"transcript is for {certificate.network!r}, "
                f"this node is {self.params.network_id!r}"
            )
        self.transcripts[certificate.match_id] = transcript

    def ingest_block(self, block: Block, *, now: int | None = None) -> BlockIngest:
        """Validate, store and persist ``block``; re-base the pool on a reorg."""
        with self.write_lock:
            return self._ingest_block(block, now=now)

    def _ingest_block(self, block: Block, *, now: int | None = None) -> BlockIngest:
        if block.header.network != self.params.network_id:
            raise ProtocolError(
                f"block is for {block.header.network!r}, this node is {self.params.network_id!r}"
            )
        digest = block_hash(block.header)
        duplicate = self.chain.has(digest)
        reorg = self.chain.add_block(block, now=now)
        stored = self.chain.get(digest)
        if stored is None:
            # The parent is unknown, so the block waits in the orphan pool and
            # the peer is not penalised: gossip order is not a promise.
            return BlockIngest(
                hash=digest,
                height=block.header.height,
                accepted=False,
                duplicate=duplicate,
            )
        if reorg is not None:
            self.mempool.rebroadcast(
                reorg.orphaned_transactions,
                reorg.orphaned_proofs,
                tip_state=self.chain.state,
            )
        else:
            self.mempool.reset(self.chain.state)
        # ``reset`` keeps proofs that are still inside their claim window, and a
        # transaction can survive if the same sender has a later pending nonce.
        # Anything this block already included is settled, so drop it explicitly.
        for transaction in block.transactions:
            self.mempool.remove_transaction(txid(transaction))
        for proof in block.proofs:
            self.mempool.remove_proof(proof.proof_id)
        self.mempool.prune(self.height)
        self._persist(stored)
        return BlockIngest(
            hash=digest,
            height=stored.height,
            accepted=True,
            duplicate=duplicate,
            reorged=reorg is not None,
        )

    def _persist(self, stored: StoredBlock) -> None:
        if self.storage is None:
            return
        self.storage.save_block(stored, tip=True)
        if stored.height % self.params.snapshot_interval == 0:
            self.storage.save_snapshot(stored)

    # -- gossip and sync ----------------------------------------------------

    def known_public_key(self, address: str) -> str | None:
        """The registered public key for ``address``, if the ledger knows one."""
        account = self.chain.state.accounts.get(address)
        return account.public_key if account is not None else None

    def apply_gossip(self, envelope: GossipEnvelope) -> None:
        """Route one verified envelope to the ledger operation it names."""
        payload = envelope.payload
        if envelope.kind is GossipKind.TRANSACTION:
            self.submit_transaction(Transaction.model_validate(payload.get("transaction")))
        elif envelope.kind is GossipKind.PROOF:
            self.announce_proof(WinProof.model_validate(payload.get("proof")))
        elif envelope.kind is GossipKind.BLOCK:
            self.ingest_block(Block.model_validate(payload.get("block")))
        else:
            url = payload.get("url")
            if not isinstance(url, str) or not url:
                raise GossipError("a peer envelope needs a url")
            self.peers.add(url, address=envelope.sender, now=self.clock())

    def handle_gossip(
        self,
        envelope: GossipEnvelope,
        *,
        peer: str | None = None,
        public_key: str | None = None,
    ) -> GossipResult:
        """Verify, deduplicate and apply an envelope; return the fanout plan.

        The signature is checked whenever the sender's public key is known
        (passed in, or registered in the ledger). Unknown senders may still
        relay blocks, proofs and transactions because those carry their own
        signatures and are re-validated by the ledger; their identity only
        feeds the abuse score.
        """
        if envelope.network != self.params.network_id:
            raise GossipError(
                f"envelope is for {envelope.network!r}, this node is {self.params.network_id!r}"
            )
        key = public_key or self.known_public_key(envelope.sender)
        if key is not None:
            verify_envelope(envelope, network=self.params.network_id, public_key=key)
        object_id = object_id_of(envelope.kind, envelope.payload)
        duplicate = not self.gossip.check_and_add(envelope.kind, object_id, now=self.clock())
        if not duplicate:
            self.apply_gossip(envelope)
        exclude = (peer,) if peer is not None else ()
        targets = choose_fanout(
            self.peers.eligible(now=self.clock()),
            rng=self.rng,
            count=self.fanout,
            exclude=exclude,
        )
        return GossipResult(
            network=self.params.network_id,
            kind=str(envelope.kind),
            object_id=object_id,
            duplicate=duplicate,
            fanout=targets,
        )

    def sync_from(self, peer: PeerClient, *, url: str) -> SyncReport:
        """Pull every missing block from ``peer`` and adopt the heavier branch."""
        try:
            document = peer.status()
            if document.get("network") != self.params.network_id:
                raise GossipError(
                    f"peer {url} is on {document.get('network')!r}, "
                    f"this node is {self.params.network_id!r}"
                )
            remote_height = document.get("height")
            if not isinstance(remote_height, int) or isinstance(remote_height, bool):
                raise GossipError(f"peer {url} reported a bad height {remote_height!r}")
        except GossipError:
            self.peers.record_failure(url, now=self.clock())
            raise
        start_height = self.height
        start, end = plan_sync(start_height, remote_height, max_blocks=self.max_sync_blocks)
        requested = max(0, end - start + 1)
        adopted = 0
        orphaned = 0
        self.syncing = True
        try:
            blocks = fetch_blocks(peer, start_height=start, end_height=end) if requested else []
            for block in blocks:
                before = self.height
                try:
                    result = self.ingest_block(block)
                except LedgerError:
                    self.peers.record_failure(url, now=self.clock())
                    raise
                if self.height > before:
                    adopted += 1
                elif not result.accepted:
                    orphaned += 1
        except GossipError:
            self.peers.record_failure(url, now=self.clock())
            raise
        finally:
            self.syncing = False
        self.peers.record_success(url, height=remote_height, now=self.clock())
        return SyncReport(
            peer=url,
            start_height=start_height,
            requested=requested,
            adopted=adopted,
            orphaned=orphaned,
            height=self.height,
            tip_hash=self.tip_hash,
        )


class HttpPeerClient:
    """``PeerClient`` over HTTP, using an injected ``httpx.Client``.

    Passing ``httpx.Client(transport=sccp.transport.ASGITransport(app))`` makes
    the same code talk to an in-process node, which is how the sync tests run
    without opening a port (httpx's own ``ASGITransport`` is async-only).
    """

    def __init__(
        self,
        base_url: str,
        *,
        client: httpx.Client | None = None,
        timeout: float = 10.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = client if client is not None else httpx.Client(timeout=timeout)
        self._owns_client = client is None

    def status(self) -> Mapping[str, object]:
        """Fetch the peer's status document."""
        return self._get_json("/v1/status")

    def block(self, reference: str) -> Mapping[str, object]:
        """Fetch one block by height or hash."""
        return self._get_json(f"/v1/blocks/{reference}")

    def _get_json(self, path: str) -> Mapping[str, object]:
        try:
            response = self._client.get(f"{self.base_url}{path}")
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise GossipError(f"cannot fetch {path} from {self.base_url}: {exc}") from exc
        document = response.json()
        if not isinstance(document, Mapping):
            raise GossipError(f"{path} did not return a JSON object")
        return document

    def close(self) -> None:
        """Close the underlying HTTP client when this object created it."""
        if self._owns_client:
            self._client.close()


def sync_peers(
    service: NodeService,
    urls: Sequence[str],
    *,
    client_factory: Callable[[str], HttpPeerClient] = HttpPeerClient,
) -> tuple[SyncReport, ...]:
    """Pull-sync from each peer in turn, skipping peers that are down.

    ``sccp node run --peer URL`` is a best-effort bootstrap: a peer that is
    unreachable when the node starts must not stop the node from serving. The
    peer table already records the failure (and bans a peer that keeps failing),
    so this returns only the reports that completed and lets the caller decide
    what to log.
    """
    reports: list[SyncReport] = []
    for url in urls:
        client = client_factory(url)
        try:
            reports.append(service.sync_from(client, url=url))
        except GossipError:
            continue
        finally:
            client.close()
    return tuple(reports)


__all__ = [
    "GENESIS_DIR_ENV",
    "HttpPeerClient",
    "NodeService",
    "default_genesis_path",
    "sync_peers",
    "unix_now",
]
