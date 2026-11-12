"""Build and publish the bootstrap chain for the two-miner testnet stack.

A fresh testnet cannot mine its first proof: availability is committed two epochs
ahead, so somebody has to build the empty bootstrap blocks, fund two miners,
register them, and commit/reveal availability before a relay has a matchable
epoch. This script does exactly that once, deterministically, against a running
node, and then writes the relay config from the epoch the chain opened.

It is a testnet convenience, not a miner and not a consensus shortcut. Every
block is built with the same :class:`~sccp.miner.block_builder.BlockBuilder` a
miner uses, submitted through the node's public ``POST /v1/blocks``, and
validated by the node like any other block. The only special input is the
published genesis faucet key, which funds the two stack miners.

Usage::

    python deploy/bootstrap_testnet_stack.py --stack /stack --node http://node:8645

The node must be at height 0 (a fresh volume) or already past the bootstrap
window. Reset the ``node-data`` volume before a full redeploy.
"""

from __future__ import annotations

import argparse
import json
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import httpx

from sccp.chain.genesis import faucet_keypair, zero_address
from sccp.chain.mempool import Mempool
from sccp.chain.state import epoch_start_height
from sccp.chess.engines import EngineError, engine_fingerprint
from sccp.config import TESTNET, NetworkParams, coins_to_units
from sccp.crypto.keys import Keypair
from sccp.crypto.wallet import load_wallet
from sccp.errors import SccpError
from sccp.miner.block_builder import BlockBuilder
from sccp.node.service import NodeService
from sccp.protocol.models import Block, Transaction
from sccp.wallet.service import WalletService

BOOTSTRAP_EPOCH: Final = 3
"""The epoch whose roster the relay starts with.

Epoch 2 is the first matchable one. Waiting for epoch 3 also means its proofs are
claimable by the first post-bootstrap block, and gives the deployment one
committed reveal path beyond the minimum.
"""

BOOTSTRAP_HEIGHT: Final = epoch_start_height(BOOTSTRAP_EPOCH + 1, TESTNET) - 1
"""Last bootstrap block: the last height of ``BOOTSTRAP_EPOCH``."""

FUNDING: Final = coins_to_units(100)
"""Coins sent to each stack miner; the minimum bond is ten coins."""

DEFAULT_ENGINE_PATH: Final = Path("/usr/games/stockfish")
"""Where Debian's ``stockfish`` package installs the binary in the image."""

ROUND_SECONDS: Final = 60
"""Open a fresh round every minute, up to the epoch's three games."""


class BootstrapError(RuntimeError):
    """The deterministic bootstrap chain could not be built or published."""


@dataclass(frozen=True, slots=True)
class BootstrapMiner:
    """One stack identity, loaded from the keyfile ``init`` wrote."""

    name: str
    address: str
    keypair: Keypair
    nonce_secret: str


def load_stack(directory: Path) -> tuple[BootstrapMiner, ...]:
    """Read ``stack.json`` and unlock each keyfile with the stack passphrase."""
    summary_path = directory / "stack.json"
    passphrase_path = directory / "passphrase"
    try:
        document = json.loads(summary_path.read_text(encoding="utf-8"))
        passphrase = passphrase_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise BootstrapError(f"cannot read the stack in {directory}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise BootstrapError(f"{summary_path} is not valid JSON: {exc}") from exc
    if not isinstance(document, Mapping):
        raise BootstrapError(f"{summary_path} must contain an object")
    if document.get("network") != TESTNET.network_id:
        raise BootstrapError(f"{summary_path} is not a {TESTNET.network_id} stack")
    raw_miners = document.get("miners")
    if not isinstance(raw_miners, list) or len(raw_miners) != 2:
        raise BootstrapError("the stack must contain exactly two miners")
    if not passphrase:
        raise BootstrapError(f"{passphrase_path} is empty")

    miners: list[BootstrapMiner] = []
    for raw in raw_miners:
        if not isinstance(raw, Mapping):
            raise BootstrapError("each stack miner must be an object")
        name = raw.get("name")
        address = raw.get("address")
        keyfile = raw.get("keyfile")
        nonce_secret = raw.get("nonce_secret")
        fields = (name, address, keyfile, nonce_secret)
        if not all(isinstance(item, str) and item for item in fields):
            raise BootstrapError("each stack miner needs a name, address, keyfile and nonce")
        assert isinstance(name, str)
        assert isinstance(address, str)
        assert isinstance(keyfile, str)
        assert isinstance(nonce_secret, str)
        try:
            wallet = load_wallet(Path(keyfile), passphrase)
        except SccpError as exc:
            raise BootstrapError(f"cannot unlock {keyfile}: {exc}") from exc
        if wallet.address != address:
            raise BootstrapError(f"{keyfile} holds {wallet.address}, stack.json says {address}")
        miners.append(
            BootstrapMiner(
                name=name,
                address=address,
                keypair=wallet.keypair,
                nonce_secret=nonce_secret,
            )
        )
    return tuple(miners)


def _tip_timestamp(service: NodeService) -> int:
    return service.chain.state.timestamps[-1] + service.params.target_block_seconds


def _add(pool: Mempool, tx: Transaction) -> None:
    """Admit ``tx`` to the temporary block pool, preserving nonce order."""
    try:
        pool.add_transaction(tx)
    except SccpError as exc:
        raise BootstrapError(f"the bootstrap transaction was refused: {exc}") from exc


def _transactions_for(
    height: int,
    *,
    service: NodeService,
    pool: Mempool,
    miners: Sequence[BootstrapMiner],
    faucet: Keypair,
    engine: str,
) -> list[Transaction]:
    """The deterministic transactions that belong in bootstrap block ``height``."""
    params = service.params
    transactions: list[Transaction] = []

    def wallet() -> WalletService:
        return WalletService(params, pool.projection)

    def transfer() -> None:
        for miner in miners:
            tx = wallet().build_transfer(faucet, miner.address, FUNDING, fee=params.min_fee)
            _add(pool, tx)
            transactions.append(tx)

    def register() -> None:
        for miner in miners:
            tx = wallet().build_register(
                miner.keypair,
                endpoint=f"http://{miner.name}:8647",
                engine=engine,
                bond=params.min_bond,
            )
            _add(pool, tx)
            transactions.append(tx)

    def availability() -> None:
        for miner in miners:
            tx = wallet().build_availability(
                miner.keypair,
                nonce_secret=miner.nonce_secret,
                max_games=params.rounds_per_epoch,
            )
            _add(pool, tx)
            transactions.append(tx)

    def reveal() -> None:
        for miner in miners:
            tx = wallet().build_reveal(miner.keypair, miner.nonce_secret)
            _add(pool, tx)
            transactions.append(tx)

    actions = {
        1: transfer,
        2: register,
        3: availability,
        5: reveal,
        6: availability,
        10: reveal,
        11: availability,
        15: reveal,
        16: availability,
    }
    action = actions.get(height)
    if action is not None:
        action()
    return transactions


def build_chain(
    miners: Sequence[BootstrapMiner],
    *,
    engine: str,
    params: NetworkParams = TESTNET,
) -> tuple[NodeService, tuple[Block, ...], dict[str, object]]:
    """Build the bootstrap blocks offline and return the relay config they imply."""
    service = NodeService.open(params)
    faucet = faucet_keypair(params.network_id)
    blocks: list[Block] = []
    for height in range(1, BOOTSTRAP_HEIGHT + 1):
        pool = Mempool(params=params, tip_state=service.chain.state)
        transactions = _transactions_for(
            height,
            service=service,
            pool=pool,
            miners=miners,
            faucet=faucet,
            engine=engine,
        )
        if not transactions and height in (1, 2, 3, 5, 6, 10, 11, 15, 16):
            raise BootstrapError(f"bootstrap height {height} produced no transactions")
        block = BlockBuilder(
            params=params,
            chain=service.chain,
            mempool=pool,
            miner_address=zero_address(params),
        ).build(timestamp=_tip_timestamp(service))
        if block is None:
            raise BootstrapError(f"no block could be built at bootstrap height {height}")
        result = service.ingest_block(block)
        if not result.accepted or result.height != height:
            raise BootstrapError(f"bootstrap height {height} did not ingest as the new tip")
        blocks.append(block)

    record = service.chain.state.epochs.get(BOOTSTRAP_EPOCH)
    if record is None or record.beacon is None:
        raise BootstrapError(f"epoch {BOOTSTRAP_EPOCH} was never opened")
    roster: dict[str, dict[str, object]] = {}
    for address, member in sorted(record.roster.items()):
        roster[address] = {
            "public_key": member.public_key,
            "engine": member.engine,
            "max_games": member.max_games,
        }
    if len(roster) != len(miners):
        raise BootstrapError(
            f"epoch {BOOTSTRAP_EPOCH} has {len(roster)} roster entries, expected {len(miners)}"
        )
    relay: dict[str, object] = {
        "network": params.network_id,
        "beacon": record.beacon.model_dump(mode="json"),
        "roster": roster,
        "timeout_seconds": 900,
        "round_seconds": ROUND_SECONDS,
        "open_round": 0,
    }
    return service, tuple(blocks), relay


def _node_height(client: httpx.Client, *, wait_seconds: float) -> int:
    """Wait for the node to answer ``/v1/status`` and return its height."""
    deadline = time.monotonic() + wait_seconds
    last_error = "no response"
    while time.monotonic() < deadline:
        try:
            response = client.get("/v1/status")
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, Mapping):
                raise BootstrapError("/v1/status did not return an object")
            height = payload.get("height")
            if not isinstance(height, int) or isinstance(height, bool) or height < 0:
                raise BootstrapError("/v1/status returned an invalid height")
            return height
        except (httpx.HTTPError, ValueError, BootstrapError) as exc:
            last_error = str(exc)
            time.sleep(2)
    raise BootstrapError(f"node did not become ready within {wait_seconds:g}s: {last_error}")


def publish_blocks(
    blocks: Sequence[Block],
    *,
    base_url: str,
    wait_seconds: float = 180.0,
) -> None:
    """Submit ``blocks`` in order to a fresh node, or verify it already has them."""
    with httpx.Client(base_url=base_url, timeout=30) as client:
        height = _node_height(client, wait_seconds=wait_seconds)
        if height >= BOOTSTRAP_HEIGHT:
            print(f"node is already at height {height}; bootstrap blocks are in place")
            return
        if height != 0:
            raise BootstrapError(
                f"node is at height {height}; reset the node-data volume before bootstrapping"
            )
        for block in blocks:
            response = client.post("/v1/blocks", json=block.model_dump(mode="json"))
            if response.status_code >= 300:
                raise BootstrapError(
                    f"block {block.header.height} was refused: "
                    f"HTTP {response.status_code} {response.text}"
                )
            if block.header.height % 5 == 0:
                print(f"published bootstrap height {block.header.height}/{BOOTSTRAP_HEIGHT}")
        final = _node_height(client, wait_seconds=30)
        if final != BOOTSTRAP_HEIGHT:
            raise BootstrapError(f"node ended at height {final}, expected {BOOTSTRAP_HEIGHT}")


def write_relay(document: Mapping[str, Any], path: Path) -> None:
    """Write the relay config derived from the published chain."""
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> None:
    """Parse arguments, build the chain and publish it unless ``--dry-run``."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stack", type=Path, default=Path("/stack"), help="Stack directory.")
    parser.add_argument(
        "--node",
        default="http://node:8645",
        help="Base URL of the node that receives the bootstrap blocks.",
    )
    parser.add_argument(
        "--engine-path",
        type=Path,
        default=DEFAULT_ENGINE_PATH,
        help="Engine binary whose fingerprint the miners register.",
    )
    parser.add_argument(
        "--relay-out",
        type=Path,
        default=None,
        help="Relay config path (default: <stack>/relay.json).",
    )
    parser.add_argument(
        "--wait-seconds",
        type=float,
        default=180.0,
        help="How long to wait for the node to answer before giving up.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Build the chain in memory and print its summary; publish nothing.",
    )
    arguments = parser.parse_args()
    if arguments.wait_seconds <= 0:
        parser.error("--wait-seconds must be positive")
    try:
        engine = engine_fingerprint(arguments.engine_path)
    except EngineError as exc:
        raise SystemExit(str(exc)) from exc
    miners = load_stack(arguments.stack)
    service, blocks, relay = build_chain(miners, engine=engine)
    try:
        print(
            f"built {len(blocks)} bootstrap blocks: height {service.height}, tip {service.tip_hash}"
        )
        beacon = relay["beacon"]
        assert isinstance(beacon, Mapping)
        roster = relay["roster"]
        assert isinstance(roster, Mapping)
        print(f"relay epoch {beacon['epoch']} with {len(roster)} roster entries")
        if arguments.dry_run:
            return
        publish_blocks(blocks, base_url=arguments.node, wait_seconds=arguments.wait_seconds)
        relay_out = (
            arguments.relay_out
            if arguments.relay_out is not None
            else arguments.stack / "relay.json"
        )
        write_relay(relay, relay_out)
        print(f"relay config written to {relay_out}")
    finally:
        service.close()


if __name__ == "__main__":
    main()
