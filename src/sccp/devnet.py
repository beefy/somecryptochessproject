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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

import httpx

from sccp.chain.block import required_proofs
from sccp.chain.genesis import faucet_keypair
from sccp.chain.mempool import Mempool
from sccp.chess.engines import Engine, MockEngine
from sccp.chess.openings import OpeningBook, book_for
from sccp.config import DEVNET, NetworkParams
from sccp.crypto.address import encode_address
from sccp.crypto.keys import Keypair, keypair_from_seed
from sccp.errors import DevnetError, MempoolError, ProtocolError, RelayError
from sccp.miner.block_builder import BlockBuilder
from sccp.miner.engine_miner import EngineMiner, MinerConfig, NodeClient, RelayClient
from sccp.miner.matchmaker import Matchmaker
from sccp.miner.matchmaker import create_app as create_matchmaker_app
from sccp.node.app import create_app as create_node_app
from sccp.node.service import NodeService, default_genesis_path
from sccp.protocol.models import WinProof
from sccp.protocol.pairing import RosterEntry
from sccp.wallet.service import WalletService

FUNDING = 100 * 100_000_000
"""Coins every devnet miner is funded with, in base units."""

MAX_PLAY_ROUNDS = 8
"""Safety bound on relay rounds per epoch."""

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
    for participant in participants:
        for build in (
            lambda wallet: wallet.build_availability(
                participant.keypair,
                nonce_secret=participant.nonce_secret,
                max_games=service.params.rounds_per_epoch,
            ),
            lambda wallet: wallet.build_reveal(participant.keypair, participant.nonce_secret),
        ):
            wallet = WalletService(service.params, service.mempool.projection)
            try:
                service.mempool.add_transaction(build(wallet))
            except (MempoolError, ProtocolError, RelayError):
                continue


def _relay_for_epoch(
    service: NodeService, epoch: int, participants: Sequence[DevnetMiner]
) -> _RelayPair:
    state = service.chain.state
    record = state.epochs.get(epoch)
    if record is None or record.beacon is None or not record.matchable:
        raise DevnetError(f"epoch {epoch} is not matchable")
    roster = {
        address: RosterEntry(address=address, max_games=member.max_games)
        for address, member in record.roster.items()
    }
    engines = {
        participant.address: participant.engine.fingerprint for participant in participants
    }
    relay = Matchmaker(
        service.params,
        beacon=record.beacon,
        roster=roster,
        public_keys=state.public_keys(),
        engines=engines,
        book=service.chain.book,
        clock=lambda: RELAY_CLOCK,
        timeout_seconds=3600,
    )
    relay_http = httpx.Client(
        transport=httpx.ASGITransport(app=create_matchmaker_app(relay)),
        base_url="http://relay",
    )
    node_http = httpx.Client(
        transport=httpx.ASGITransport(app=create_node_app(service)),
        base_url="http://node",
    )
    return _RelayPair(
        relay=relay,
        relay_client=RelayClient("http://relay", client=relay_http),
        node_client=NodeClient("http://node", client=node_http),
        http=[relay_http, node_http],
    )
