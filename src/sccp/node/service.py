"""The node: chain, mempool, transcripts and gossip in one object (section 5.12).

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
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from sccp.chain.block import block_hash
from sccp.chain.chain import Chain, StoredBlock
from sccp.chain.genesis import load_genesis
from sccp.chain.mempool import Mempool
from sccp.chain.storage import Storage, restore_chain
from sccp.chain.tx import txid
from sccp.chess.openings import OpeningBook
from sccp.config import NetworkParams, get_network
from sccp.crypto.address import validate_address
from sccp.errors import (
    AddressError,
    GenesisError,
    GossipError,
    LedgerError,
    NotFoundError,
    ProtocolError,
)
from sccp.node.models import (
    AccountView,
    BlockIngest,
    BlockView,
    DifficultyPoint,
    DifficultyView,
    EpochView,
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
from sccp.protocol.models import Beacon, Block, Transaction, Transcript, WinProof
from sccp.version import PROTOCOL_VERSION

GENESIS_DIR_ENV = "SCCP_GENESIS_DIR"
"""Environment variable pointing at a directory of ``<network>.json`` files."""


def unix_now() -> int:
    """Wall-clock seconds; injected in tests so nothing sleeps."""
    return int(time.time())


def default_genesis_path(network: str) -> Path:
    """Find ``<network>.json`` in ``$SCCP_GENESIS_DIR``, ``./genesis`` or the repo."""
    candidates: list[Path] = []
    configured = os.environ.get(GENESIS_DIR_ENV)
    if configured:
        candidates.append(Path(configured))
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
        does not run an engine, it only checks proof quality under the sampled
        policy (M5). This method is where that check will land.
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
            hash=stored.block_hash,
            parent_hash=stored.parent_hash,
            height=stored.height,
            chain_work=stored.chain_work,
            main_chain=self.chain.is_main_chain(stored.block_hash),
            block=stored.block,
        )

    def account(self, address: str) -> AccountView:
        """Balance, bond, nonce and registration for ``address``."""
        if not validate_address(address, self.params):
            raise AddressError(f"{address!r} is not a {self.params.network_id} address")
        account = self.chain.state.account(address)
        return AccountView(
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
        record = self.chain.state.proofs.get(game_hash)
        if record is not None:
            transcript = self._transcript_for_game(game_hash)
            return GameView(
                game_hash=game_hash,
                network=self.params.network_id,
                proof_id=record.proof_id,
                match_id=transcript.certificate.match_id if transcript else None,
                winner=record.winner,
                height=record.height,
            )
        for proof in self.mempool.proofs.values():
            if proof.game_hash == game_hash:
                return GameView(
                    game_hash=game_hash,
                    network=self.params.network_id,
                    proof_id=proof.proof_id,
                    match_id=proof.match_id,
                    winner=proof.winner,
                    in_mempool=True,
                )
        raise NotFoundError(f"unknown game {game_hash!r}")

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
        digest = self.mempool.add_transaction(tx)
        return TxAccepted(network=self.params.network_id, txid=digest)

    def announce_proof(self, proof: WinProof) -> ProofAccepted:
        """Admit a win proof after the same checks a block would apply."""
        if proof.network != self.params.network_id:
            raise ProtocolError(
                f"proof is for {proof.network!r}, this node is {self.params.network_id!r}"
            )
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
        document = peer.status()
        if document.get("network") != self.params.network_id:
            raise GossipError(
                f"peer {url} is on {document.get('network')!r}, "
                f"this node is {self.params.network_id!r}"
            )
        remote_height = document.get("height")
        if not isinstance(remote_height, int) or isinstance(remote_height, bool):
            raise GossipError(f"peer {url} reported a bad height {remote_height!r}")
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


__all__ = [
    "GENESIS_DIR_ENV",
    "HttpPeerClient",
    "NodeService",
    "default_genesis_path",
    "unix_now",
]
