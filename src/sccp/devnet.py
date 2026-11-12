"""``sccp devnet up``: a whole network in one process (plan M4).

The devnet is built bottom-up from the same library calls a real deployment
uses: registrations, availability commitments and reveals are signed
transactions, the relay pairs and certifies real matches, miners play them
through the relay's HTTP API, winners announce verified proofs to the node, and
the node mines blocks with the production block builder.

Two things make it feasible in one command:

- bootstrap is consensus-defined: blocks up to ``bootstrap_end_height`` may be
  empty, and that window is exactly long enough to fund, register and commit
  availability;
- everything is in-process. The relay and the node are FastAPI apps reached
  through ``httpx`` ASGI transports, so no port is opened and a test can run a
  full network in milliseconds.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

import httpx

from sccp.chain.genesis import faucet_keypair
from sccp.chain.mempool import Mempool
from sccp.chess.engines import Engine, MockEngine
from sccp.config import DEVNET
from sccp.crypto.address import encode_address
from sccp.crypto.keys import Keypair, keypair_from_seed
from sccp.errors import DevnetError, MempoolError, ProtocolError, RelayError
from sccp.miner.block_builder import BlockBuilder
from sccp.miner.engine_miner import EngineMiner, MinerConfig, NodeClient, RelayClient
from sccp.miner.matchmaker import Matchmaker, ResignRequest
from sccp.miner.matchmaker import create_app as create_matchmaker_app
from sccp.node.app import create_app as create_node_app
from sccp.node.service import NodeService
from sccp.protocol.models import Transaction, WinProof
from sccp.protocol.pairing import RosterEntry
from sccp.transport import ASGITransport
from sccp.wallet.service import WalletService

FUNDING = 100 * 100_000_000
"""Coins every devnet miner is funded with, in base units."""

MAX_PLAY_ITERATIONS = 2000
"""Safety bound on miner polling iterations inside one round."""

RELAY_CLOCK = 1_700_000_000
"""A frozen relay clock: devnet games must never expire while a test runs."""

GENESIS_EPOCH = 3
"""The first epoch a fresh devnet can pair in (see ``bootstrap_end_height``)."""


@dataclass(frozen=True, slots=True)
class DevnetSummary:
    """What ``sccp devnet up`` produced."""

    network: str
    miners: int
    blocks: int
    height: int
    tip_hash: str
    genesis_hash: str
    epochs: tuple[int, ...]
    matches: tuple[str, ...]
    proofs: tuple[str, ...]
    balances: dict[str, int]
    seconds: float

    def as_dict(self) -> dict[str, object]:
        """A JSON-friendly form for ``sccp devnet up --json``."""
        return {
            "network": self.network,
            "miners": self.miners,
            "blocks": self.blocks,
            "height": self.height,
            "tip_hash": self.tip_hash,
            "genesis_hash": self.genesis_hash,
            "epochs": list(self.epochs),
            "matches": list(self.matches),
            "proofs": list(self.proofs),
            "balances": dict(self.balances),
            "seconds": self.seconds,
        }


@dataclass(slots=True)
class DevnetMiner:
    """One devnet participant: keys, engine, availability secret."""

    keypair: Keypair
    engine: Engine
    nonce_secret: str

    @property
    def address(self) -> str:
        """The miner's devnet address."""
        return encode_address(self.keypair.public_key_bytes, DEVNET)


@dataclass(slots=True)
class _RelayPair:
    """The in-process relay plus the transports that reach it and the node."""

    relay: Matchmaker
    relay_client: RelayClient
    node_client: NodeClient
    http: list[httpx.Client] = field(default_factory=list)

    def close(self) -> None:
        for client in self.http:
            client.close()


def _participants(count: int) -> list[DevnetMiner]:
    """Deterministic devnet keys and seeded mock engines."""
    return [
        DevnetMiner(
            keypair=keypair_from_seed(bytes([index + 1]) * 32),
            engine=MockEngine(seed=index + 1),
            nonce_secret=f"{index + 1:02x}" * 32,
        )
        for index in range(count)
    ]


def _build_and_ingest(
    service: NodeService,
    keypair: Keypair,
    *,
    pool: Mempool | None = None,
) -> None:
    """Build one block with the production builder and hand it to the node."""
    book = service.chain.book
    mempool = (
        pool
        if pool is not None
        else Mempool(params=service.params, tip_state=service.chain.state, book=book)
    )
    stamp = service.chain.state.timestamps[-1] + 1
    block = BlockBuilder(
        params=service.params,
        chain=service.chain,
        mempool=mempool,
        keypair=keypair,
    ).build(timestamp=stamp)
    if block is None:
        raise DevnetError(f"the builder refused a block at height {service.height + 1}")
    ingested = service.ingest_block(block)
    if not ingested.accepted:
        raise DevnetError(f"the node refused block {ingested.hash} at height {ingested.height}")


def _bootstrap(service: NodeService, participants: Sequence[DevnetMiner]) -> None:
    """Fund, register, commit and reveal so epochs 3 and 4 can pair."""
    params = service.params
    book = service.chain.book
    faucet = faucet_keypair(params.network_id)
    scheduler = participants[0].keypair

    pool = Mempool(params=params, tip_state=service.chain.state, book=book)
    for participant in participants:
        pool.add_transaction(
            WalletService(params, pool.projection).build_transfer(
                faucet, participant.address, FUNDING
            )
        )
    for participant in participants:
        pool.add_transaction(
            WalletService(params, pool.projection).build_register(
                participant.keypair,
                endpoint=f"tcp://{participant.address[-8:]}.devnet",
                engine=participant.engine.fingerprint,
            )
        )
    for participant in participants:
        pool.add_transaction(
            WalletService(params, pool.projection).build_availability(
                participant.keypair,
                nonce_secret=participant.nonce_secret,
                max_games=params.rounds_per_epoch,
            )
        )

    _build_and_ingest(service, scheduler, pool=pool)

    for _ in range(2):
        pool = Mempool(params=params, tip_state=service.chain.state, book=book)
        for participant in participants:
            pool.add_transaction(
                WalletService(params, pool.projection).build_reveal(
                    participant.keypair, participant.nonce_secret
                )
            )
            pool.add_transaction(
                WalletService(params, pool.projection).build_availability(
                    participant.keypair,
                    nonce_secret=participant.nonce_secret,
                    max_games=params.rounds_per_epoch,
                )
            )
        _build_and_ingest(service, scheduler, pool=pool)


def _submit_epoch_txs(service: NodeService, participants: Sequence[DevnetMiner]) -> None:
    """Best-effort availability commits and reveals for the epochs ahead."""

    def admit(tx: Transaction) -> None:
        try:
            service.mempool.add_transaction(tx)
        except (MempoolError, ProtocolError):
            return

    for participant in participants:
        wallet = WalletService(service.params, service.mempool.projection)
        admit(
            wallet.build_availability(
                participant.keypair,
                nonce_secret=participant.nonce_secret,
                max_games=service.params.rounds_per_epoch,
            )
        )
        wallet = WalletService(service.params, service.mempool.projection)
        admit(wallet.build_reveal(participant.keypair, participant.nonce_secret))


def _relay_for_epoch(
    service: NodeService,
    epoch: int,
    participants: Sequence[DevnetMiner],
    *,
    clock: Callable[[], int] | None = None,
    clock_ms: Callable[[], int] | None = None,
    timeout_seconds: int = 3600,
    witness_keypair: Keypair | None = None,
) -> _RelayPair:
    state = service.chain.state
    record = state.epochs.get(epoch)
    if record is None or record.beacon is None or not record.matchable:
        raise DevnetError(f"epoch {epoch} is not matchable")
    roster = {
        address: RosterEntry(address=address, max_games=member.max_games)
        for address, member in record.roster.items()
    }
    engines = {participant.address: participant.engine.fingerprint for participant in participants}
    relay = Matchmaker(
        service.params,
        beacon=record.beacon,
        roster=roster,
        public_keys=state.public_keys(),
        engines=engines,
        book=service.chain.book,
        clock=clock if clock is not None else (lambda: RELAY_CLOCK),
        clock_ms=clock_ms if clock_ms is not None else (lambda: RELAY_CLOCK * 1000),
        timeout_seconds=timeout_seconds,
        witness_keypair=witness_keypair,
    )
    relay_http = httpx.Client(
        transport=ASGITransport(create_matchmaker_app(relay)),
        base_url="http://relay",
    )
    node_http = httpx.Client(
        transport=ASGITransport(create_node_app(service)),
        base_url="http://node",
    )
    return _RelayPair(
        relay=relay,
        relay_client=RelayClient("http://relay", client=relay_http),
        node_client=NodeClient("http://node", client=node_http),
        http=[relay_http, node_http],
    )


RESIGN_PLIES = 60
"""Devnet policy: end a long game by resignation so a run always yields proofs.

Two seeded mock engines can shuffle for hundreds of plies and draw, and a draw
has no winner, so it produces no proof. Resigning is a consensus-valid result
(``Termination.RESIGNATION``) that both seats can co-sign and the winner can
prove, so the devnet stops long games instead of waiting for the ply ceiling.
"""


def _all_settled(miners: Sequence[EngineMiner], pair: _RelayPair) -> bool:
    """Whether every relay session and every miner's view of it is final."""
    try:
        views = pair.relay_client.matches()
    except RelayError:
        return True
    for view in views:
        if view.status in ("pending", "active"):
            return False
        if view.result is None or len(view.result.result_signatures) < 2:
            return False
    for miner in miners:
        for state in miner.matches.values():
            if not (state.finished or state.refused):
                return False
    return True


def _resign_stalled(pair: _RelayPair) -> int:
    """Resign the side to move in games that passed ``RESIGN_PLIES``."""
    resigned = 0
    for view in pair.relay_client.matches():
        if view.status != "active" or len(view.san_moves) < RESIGN_PLIES:
            continue
        seat = view.matchup.white if len(view.san_moves) % 2 == 0 else view.matchup.black
        try:
            pair.relay.resign(view.matchup.match_id, ResignRequest(seat=seat))
        except RelayError:
            continue
        resigned += 1
    return resigned


def _play_round(miners: Sequence[EngineMiner], pair: _RelayPair) -> None:
    """Poll every miner until the round is settled or nothing can progress."""
    for _ in range(MAX_PLAY_ITERATIONS):
        if _all_settled(miners, pair):
            return
        acted = _resign_stalled(pair) > 0
        for miner in miners:
            tick = miner.run_once()
            acted = acted or bool(tick.joined or tick.moves or tick.results or tick.proofs)
        if not acted:
            return


def _play_epoch(
    service: NodeService,
    epoch: int,
    participants: Sequence[DevnetMiner],
    *,
    max_rounds: int,
) -> tuple[list[str], list[WinProof]]:
    """Open and play every round the relay's quotas still allow."""
    try:
        pair = _relay_for_epoch(service, epoch, participants)
    except DevnetError:
        return [], []
    miners = [
        EngineMiner(
            MinerConfig(
                params=service.params,
                keypair=participant.keypair,
                max_games_per_epoch=service.params.rounds_per_epoch,
            ),
            engine=participant.engine,
            relay=pair.relay_client,
            node=pair.node_client,
        )
        for participant in participants
    ]
    matches: list[str] = []
    proofs: list[WinProof] = []
    try:
        for round_number in range(max_rounds):
            try:
                views = pair.relay.open_round(round_number)
            except RelayError:
                break
            if not views:
                break
            matches.extend(view.matchup.match_id for view in views)
            _play_round(miners, pair)
            for miner in miners:
                proofs.extend(miner.proofs.values())
    finally:
        pair.close()
    return matches, proofs


def _rebase_mempool(service: NodeService) -> None:
    """Drop what the new tip consumed, keeping everything still admissible."""
    pending_txs = [entry.tx for entry in service.mempool.transactions.values()]
    pending_proofs = list(service.mempool.proofs.values())
    fresh = Mempool(params=service.params, tip_state=service.chain.state, book=service.chain.book)
    for tx in pending_txs:
        try:
            fresh.add_transaction(tx)
        except MempoolError:
            continue
    for proof in pending_proofs:
        try:
            fresh.add_proof(proof)
        except MempoolError:
            continue
    service.mempool = fresh


def _mine_pending(
    service: NodeService,
    keypairs: Mapping[str, Keypair],
    mined: list[str],
) -> bool:
    """Mine one block from a pending proof; return whether the height moved."""
    for proof in sorted(service.mempool.proofs.values(), key=lambda item: item.proof_id):
        keypair = keypairs.get(proof.winner)
        if keypair is None:
            continue
        before = service.height
        _build_and_ingest(service, keypair, pool=service.mempool)
        if service.height == before:
            raise DevnetError(f"block at height {before + 1} did not extend the chain")
        mined.append(proof.proof_id)
        _rebase_mempool(service)
        return True
    return False


def run_devnet(
    *,
    miners: int = 4,
    blocks: int = 10,
    genesis_path: str | None = None,
    max_epochs: int = 6,
) -> DevnetSummary:
    """Run a complete in-process network and return what it produced."""
    if miners < 2:
        raise DevnetError("a devnet needs at least two miners")
    if blocks < 1:
        raise DevnetError("a devnet needs at least one block")

    started = time.monotonic()
    params = DEVNET
    participants = _participants(miners)
    keypairs = {participant.address: participant.keypair for participant in participants}
    service = NodeService.open(params, genesis_path=genesis_path)
    service.check_startup()
    try:
        _bootstrap(service, participants)
        matches: list[str] = []
        proof_ids: list[str] = []
        epochs: list[int] = []
        epoch = GENESIS_EPOCH
        while service.height < blocks:
            _submit_epoch_txs(service, participants)
            if _mine_pending(service, keypairs, proof_ids):
                continue
            if len(epochs) >= max_epochs:
                break
            epoch_matches, _ = _play_epoch(
                service, epoch, participants, max_rounds=params.rounds_per_epoch
            )
            if not epoch_matches:
                break
            matches.extend(epoch_matches)
            epochs.append(epoch)
            epoch += 1
        if service.height < blocks:
            raise DevnetError(
                f"the devnet stalled at height {service.height} of {blocks}: "
                f"{len(matches)} matches, {len(proof_ids)} proofs mined"
            )
        state = service.chain.state
        balances = {
            participant.address: state.account(participant.address).balance
            for participant in participants
        }
        return DevnetSummary(
            network=params.network_id,
            miners=miners,
            blocks=blocks,
            height=service.height,
            tip_hash=service.chain.tip_hash,
            genesis_hash=service.chain.genesis_hash,
            epochs=tuple(epochs),
            matches=tuple(matches),
            proofs=tuple(proof_ids),
            balances=balances,
            seconds=round(time.monotonic() - started, 3),
        )
    finally:
        service.close()
