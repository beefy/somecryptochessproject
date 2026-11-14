"""Build the bootstrap chain and the relay config for the devnet stack.

A fresh chain cannot mine its first proof: availability is committed two epochs
ahead and only a reveal makes an epoch matchable, so somebody has to build the
first blocks, fund two miners, register them, and commit/reveal availability
before a relay has a roster to match. This script does exactly that, once, and
writes the relay config the chain implies next to the chain it wrote.

Unlike ``deploy/bootstrap_testnet_stack.py`` this one needs no running node: it
writes ``chain.sqlite`` in ``--data-dir``, and ``sccp node run --data-dir``
later opens the same file. Every block is built with the same
:class:`~sccp.miner.block_builder.BlockBuilder` a miner uses and validated by
:class:`~sccp.node.service.NodeService` like any other block. The only special
input is the published genesis faucet key, which funds the two stack miners.

Run ``deploy/init_devnet_stack.py`` first. The devnet bootstrap is consensus
defined (``bootstrap_end_height``), so the chain stops at height 3 with epoch 3
matchable and epoch 4's commit and reveal already on chain; the relay starts on
epoch 3.

Usage::

    python deploy/init_devnet_stack.py --out .devnet-stack
    python deploy/bootstrap_devnet_stack.py --stack .devnet-stack

Re-running over an existing stack is safe: a chain that is already bootstrapped
is left alone and its relay config is derived again. ``--fresh`` deletes the
chain first; the keyfiles are never touched. Stop the node before pointing
``--data-dir`` at a chain it has open — the script writes blocks, and two
processes building on the same SQLite file would disagree about the tip.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from sccp.chain.block import bootstrap_end_height
from sccp.chain.genesis import faucet_keypair, zero_address
from sccp.chain.mempool import Mempool
from sccp.chain.state import EpochRecord
from sccp.chain.storage import Storage
from sccp.chess.engines import MockEngine
from sccp.config import DEVNET, NetworkParams, coins_to_units
from sccp.crypto.pq import HybridKeypair
from sccp.crypto.wallet import load_wallet
from sccp.errors import SccpError
from sccp.miner.block_builder import BlockBuilder
from sccp.node.service import NodeService
from sccp.protocol.models import Block, Transaction
from sccp.wallet.service import WalletService

BOOTSTRAP_EPOCH: Final = 3
"""The epoch whose roster the relay starts with.

``bootstrap_end_height`` ends the proofless window at the last block of the
first matchable epoch, so a fresh devnet can pair in epoch 3 and, because the
bootstrap also commits and reveals for 4, in epoch 4.
"""

BOOTSTRAP_HEIGHT: Final = bootstrap_end_height(DEVNET)
"""Last bootstrap block: ``3`` on devnet (one-block epochs)."""

FUNDING: Final = coins_to_units(100)
"""Coins sent to each stack miner, so fees never run out in a demo."""

ROUND_SECONDS: Final = 60
"""Open a fresh relay round every minute, as the testnet stack does."""


class BootstrapError(RuntimeError):
    """The deterministic bootstrap chain could not be built or published."""


@dataclass(frozen=True, slots=True)
class BootstrapMiner:
    """One stack identity, loaded from the keyfile ``init`` wrote."""

    name: str
    address: str
    keypair: HybridKeypair
    nonce_secret: str
    engine: str


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
    if document.get("network") != DEVNET.network_id:
        raise BootstrapError(f"{summary_path} is not a {DEVNET.network_id} stack")
    if document.get("availability_epoch") != BOOTSTRAP_EPOCH:
        raise BootstrapError(
            f"{summary_path} targets epoch {document.get('availability_epoch')}, "
            f"this bootstrap opens epoch {BOOTSTRAP_EPOCH}"
        )
    raw_miners = document.get("miners")
    if not isinstance(raw_miners, list) or len(raw_miners) != 2:
        raise BootstrapError("the stack must contain exactly two miners")
    if not passphrase:
        raise BootstrapError(f"{passphrase_path} is empty")

    miners: list[BootstrapMiner] = []
    for index, raw in enumerate(raw_miners):
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
                keypair=wallet.signer,
                nonce_secret=nonce_secret,
                # The fingerprints ``sccp devnet up`` registers for its seeded
                # mock engines, so the chain this writes reads like a devnet-up
                # chain: the relay copies these into each match certificate.
                engine=MockEngine(seed=index + 1).fingerprint,
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
    faucet: HybridKeypair,
) -> list[Transaction]:
    """The deterministic transactions that belong in bootstrap block ``height``.

    Devnet packs a whole testnet bootstrap into three blocks because an epoch is
    one block: block 1 funds and registers both miners and commits availability
    for epoch 3, block 2 reveals for 3 and commits for 4, and block 3 reveals
    for 4 and commits for 5.
    """
    params = service.params
    transactions: list[Transaction] = []

    def wallet() -> WalletService:
        return WalletService(params, pool.projection)

    def fund_and_register() -> None:
        for miner in miners:
            tx = wallet().build_transfer(faucet, miner.address, FUNDING, fee=params.min_fee)
            _add(pool, tx)
            transactions.append(tx)
        for miner in miners:
            tx = wallet().build_register(
                miner.keypair,
                endpoint=f"tcp://{miner.name}.devnet",
                engine=miner.engine,
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

    if height == 1:
        fund_and_register()
        availability()
    else:
        reveal()
        availability()
    return transactions


def _bootstrap(service: NodeService, miners: Sequence[BootstrapMiner]) -> tuple[Block, ...]:
    """Build and ingest every bootstrap block, and return them in order."""
    params = service.params
    faucet = faucet_keypair(params.network_id)
    blocks: list[Block] = []
    for height in range(1, BOOTSTRAP_HEIGHT + 1):
        pool = Mempool(params=params, tip_state=service.chain.state)
        transactions = _transactions_for(
            height, service=service, pool=pool, miners=miners, faucet=faucet
        )
        if not transactions:
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
    return tuple(blocks)


def relay_epoch(service: NodeService) -> tuple[int, EpochRecord]:
    """The newest recorded epoch that has both a beacon and a roster."""
    epochs = service.chain.state.epochs
    matchable = {
        epoch: record
        for epoch, record in epochs.items()
        if record.beacon is not None and record.roster
    }
    if not matchable:
        raise BootstrapError("no epoch with a beacon and a roster yet; the chain cannot pair")
    epoch = max(matchable)
    return epoch, matchable[epoch]


def relay_document(service: NodeService, miners: Sequence[BootstrapMiner]) -> dict[str, object]:
    """The relay config implied by the chain: beacon and roster of one epoch."""
    epoch, record = relay_epoch(service)
    roster: dict[str, dict[str, object]] = {
        address: {
            "public_key": member.public_key,
            "engine": member.engine,
            "max_games": member.max_games,
        }
        for address, member in sorted(record.roster.items())
    }
    missing = sorted({miner.address for miner in miners} - set(roster))
    if missing:
        raise BootstrapError(f"epoch {epoch} has no roster entry for {', '.join(missing)}")
    assert record.beacon is not None  # relay_epoch only returns epochs with a beacon
    return {
        "network": service.params.network_id,
        "beacon": record.beacon.model_dump(mode="json"),
        "roster": roster,
        "timeout_seconds": 900,
        "round_seconds": ROUND_SECONDS,
        "open_round": 0,
    }


def build_chain(
    miners: Sequence[BootstrapMiner],
    *,
    data_dir: Path | None = None,
    params: NetworkParams = DEVNET,
) -> tuple[NodeService, tuple[Block, ...], dict[str, object]]:
    """Bootstrap the chain in ``data_dir`` and return it with its relay config.

    ``data_dir=None`` keeps the chain in memory, which is what the tests use.
    Otherwise the chain is written to ``<data_dir>/chain.sqlite``, the file
    ``sccp node run --data-dir`` opens. An already bootstrapped chain is not
    touched (the block tuple is empty); a chain stuck below the bootstrap tip is
    an error, because its nonces and blocks cannot be reproduced.

    The caller owns the returned service and must call ``close()``.
    """
    if data_dir is not None:
        data_dir.mkdir(parents=True, exist_ok=True)
    storage = None if data_dir is None else Storage(data_dir / "chain.sqlite")
    service = NodeService.open(params, storage=storage)
    try:
        blocks: tuple[Block, ...] = ()
        if service.height == 0:
            blocks = _bootstrap(service, miners)
        if service.height < BOOTSTRAP_HEIGHT:
            raise BootstrapError(
                f"the chain is at height {service.height}, below the bootstrap tip "
                f"{BOOTSTRAP_HEIGHT}; delete it or pass --fresh"
            )
        relay = relay_document(service, miners)
    except BaseException:
        service.close()
        raise
    return service, blocks, relay


def write_relay(document: Mapping[str, Any], path: Path) -> None:
    """Write the relay config derived from the published chain."""
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _remove_chain(database: Path) -> None:
    """Delete the SQLite database and its WAL sidecars, if any."""
    for suffix in ("", "-wal", "-shm"):
        candidate = database.with_name(database.name + suffix)
        if candidate.exists():
            candidate.unlink()


def _print_next_steps(
    stack: Path,
    data_dir: Path,
    relay_out: Path,
    miners: Sequence[BootstrapMiner],
    epoch: int,
) -> None:
    """Print the commands that turn the files into a running stack."""
    passphrase = stack / "passphrase"
    print("start the node, then the relay and one miner per keyfile:")
    print(
        f"  sccp --network devnet --data-dir {data_dir} node run --mine "
        f"--miner-address {miners[0].address} --no-mine-bootstrap"
    )
    print(
        f"  sccp --network devnet matchmaker run --config {relay_out} --port 8646 "
        "--origin http://127.0.0.1:8645 --node http://127.0.0.1:8645"
    )
    for miner in miners:
        print(
            f"  sccp --network devnet --passphrase-file {passphrase} miner run "
            f"--key {stack / 'keys' / (miner.name + '.key')} "
            f"--relay http://127.0.0.1:8646 --node http://127.0.0.1:8645 "
            f"--nonce-secret-file {stack / 'keys' / (miner.name + '.nonce')} --engine-profile mock"
        )
    print(f"the relay follows the chain from epoch {epoch}; Ctrl+C stops each process")


def main() -> None:
    """Parse arguments, bootstrap the chain and write the relay config."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--stack",
        type=Path,
        default=Path(".devnet-stack"),
        help="Stack directory written by init_devnet_stack.py.",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="Chain directory (default: <stack>/chain).",
    )
    parser.add_argument(
        "--relay-out",
        type=Path,
        default=None,
        help="Relay config path (default: <stack>/relay.json).",
    )
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="Delete the chain database (never the keys) before bootstrapping.",
    )
    arguments = parser.parse_args()

    data_dir = arguments.stack / "chain" if arguments.data_dir is None else arguments.data_dir
    relay_out = (
        arguments.stack / "relay.json" if arguments.relay_out is None else arguments.relay_out
    )
    try:
        miners = load_stack(arguments.stack)
        if arguments.fresh:
            _remove_chain(data_dir / "chain.sqlite")
        service, blocks, relay = build_chain(miners, data_dir=data_dir)
    except BootstrapError as exc:
        raise SystemExit(f"error: {exc}") from exc
    try:
        epoch, _record = relay_epoch(service)
        write_relay(relay, relay_out)
        if blocks:
            print(
                f"built {len(blocks)} bootstrap blocks: height {service.height}, "
                f"tip {service.tip_hash}"
            )
        else:
            print(f"chain already bootstrapped at height {service.height}; left untouched")
        print(f"relay config for epoch {epoch} written to {relay_out}")
        _print_next_steps(arguments.stack, data_dir, relay_out, miners, epoch)
    finally:
        service.close()


if __name__ == "__main__":
    main()
