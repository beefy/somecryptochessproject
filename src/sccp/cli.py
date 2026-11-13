"""``sccp`` command line interface.

The CLI exposes the wallet and keyfile tools, offline ``verify pgn`` and
``verify proof`` checks, the matchmaking relay, the node (``node run``), the
unattended miner (``miner run``, ``miner register``, ``miner availability``,
``miner reveal``), the read-only chain commands, the testnet faucet and the
one-command devnet. Every command is a thin wrapper over a library call so the
same behaviour is testable in-process.

Global options may be passed before or after the subcommand; an explicitly
passed ``--network`` always wins over the global default of ``testnet``.
"""

from __future__ import annotations

import json as jsonlib
import os
import secrets
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Thread
from typing import Annotated, NoReturn, TypeVar

import typer

from sccp import version as version_module
from sccp.chain.genesis import faucet_keypair
from sccp.chain.storage import Storage
from sccp.chain.tx import txid
from sccp.chess.engines import open_engine
from sccp.chess.openings import load_book
from sccp.chess.pgn import GameResult, validate_pgn_file
from sccp.config import NetworkParams, coins_to_units, format_coins, get_network
from sccp.crypto.address import encode_address, validate_address
from sccp.crypto.keys import Keypair
from sccp.crypto.wallet import (
    DEFAULT_KDF,
    PQ_KDF,
    SCHEME_ED25519,
    SCHEME_HYBRID,
    SCHEMES,
    KdfParams,
    Wallet,
    create_wallet,
    export_mnemonic,
    import_mnemonic,
    load_wallet,
    read_header,
    read_passphrase_file,
)
from sccp.devnet import run_devnet
from sccp.errors import SccpError, WalletServiceError
from sccp.miner.engine_miner import EngineMiner, MinerConfig, NodeClient
from sccp.miner.epoch_follower import EpochFollower
from sccp.miner.matchmaker import Matchmaker
from sccp.miner.matchmaker import create_app as create_matchmaker_app
from sccp.node.app import create_app as create_node_app
from sccp.node.app import web_dist_root
from sccp.node.miner import NodeMiner, proof_summary
from sccp.node.service import NodeService, sync_peers
from sccp.protocol.models import Transaction, WinProof
from sccp.protocol.verification import (
    ProofContext,
    context_from_document,
    verify_win_proof,
)
from sccp.wallet.service import WalletService

DEFAULT_NETWORK = "testnet"
PASSPHRASE_ENV = "SCCP_WALLET_PASSPHRASE"
DEFAULT_NODE_URL = "http://127.0.0.1:8645"
FAUCET_NETWORKS = ("testnet", "devnet")
"Networks whose genesis funds a public faucet."

T = TypeVar("T")

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="A cryptocurrency whose proof of work is winning chess games.",
)
wallet_app = typer.Typer(no_args_is_help=True, help="Create, inspect and back up wallets.")
app.add_typer(wallet_app, name="wallet")
verify_app = typer.Typer(no_args_is_help=True, help="Verify games and proofs offline.")
app.add_typer(verify_app, name="verify")
matchmaker_app = typer.Typer(no_args_is_help=True, help="Run and inspect the matchmaking relay.")
app.add_typer(matchmaker_app, name="matchmaker")
node_app = typer.Typer(no_args_is_help=True, help="Run and query a full node.")
app.add_typer(node_app, name="node")
miner_app = typer.Typer(no_args_is_help=True, help="Run a miner and manage its on-chain keys.")
app.add_typer(miner_app, name="miner")
chain_app = typer.Typer(no_args_is_help=True, help="Read the chain stored in --data-dir.")
app.add_typer(chain_app, name="chain")
devnet_app = typer.Typer(no_args_is_help=True, help="Run a whole network in one process.")
app.add_typer(devnet_app, name="devnet")
faucet_app = typer.Typer(no_args_is_help=True, help="Send testnet coins from the public faucet.")
app.add_typer(faucet_app, name="faucet")


@dataclass(frozen=True, slots=True)
class GlobalOptions:
    """Values supplied by the global options, before per-command overrides."""

    network: str | None
    data_dir: Path
    passphrase_file: Path | None
    json_output: bool


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(version_module.version_string())
        raise typer.Exit()


@app.callback()
def main(
    ctx: typer.Context,
    network: Annotated[
        str | None,
        typer.Option("--network", help="Network id: mainnet, testnet or devnet."),
    ] = None,
    data_dir: Annotated[
        Path,
        typer.Option("--data-dir", help="Directory for chain and wallet data."),
    ] = Path("./data"),
    passphrase_file: Annotated[
        Path | None,
        typer.Option(
            "--passphrase-file",
            help="File holding the wallet passphrase (mode 0600, first line used).",
        ),
    ] = None,
    log_level: Annotated[
        str,
        typer.Option("--log-level", help="Logging verbosity (info, debug, ...)."),
    ] = "info",
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print machine-readable JSON."),
    ] = False,
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            "-V",
            callback=_version_callback,
            is_eager=True,
            help="Show the package and protocol version, then exit.",
        ),
    ] = False,
) -> None:
    """Shared setup: verify the chess library, validate the network, store options."""
    version_module.verify_chess_version()
    if network is not None:
        try:
            get_network(network)
        except SccpError as exc:
            _fail(str(exc), code=2)
    ctx.obj = GlobalOptions(
        network=network,
        data_dir=data_dir,
        passphrase_file=passphrase_file,
        json_output=json_output,
    )


def _options(ctx: typer.Context) -> GlobalOptions:
    options = ctx.obj
    if not isinstance(options, GlobalOptions):  # pragma: no cover - callback always sets it
        raise typer.Exit(code=2)
    return options


def _fail(message: str, code: int = 1) -> NoReturn:
    typer.secho(f"error: {message}", err=True)
    raise typer.Exit(code=code)


def _run(action: Callable[[], T]) -> T:
    """Run a library call, turning typed errors into a clean CLI exit."""
    try:
        return action()
    except SccpError as exc:
        _fail(str(exc))


def _resolve_network(ctx: typer.Context, network: str | None) -> NetworkParams:
    options = _options(ctx)
    return _run(lambda: get_network(network or options.network or DEFAULT_NETWORK))


def _check_network(ctx: typer.Context, network: str | None, header_network: str) -> None:
    """Refuse to operate on a keyfile belonging to a different network."""
    explicit = network or _options(ctx).network
    if explicit is None:
        return
    requested = _run(lambda: get_network(explicit))
    if header_network != requested.network_id:
        _fail(
            f"keyfile belongs to network {header_network!r}, "
            f"but --network {requested.network_id!r} was requested"
        )


def _passphrase(ctx: typer.Context, *, confirm: bool) -> str:
    options = _options(ctx)
    passphrase_file = options.passphrase_file
    if passphrase_file is not None:
        return _run(lambda: read_passphrase_file(passphrase_file))
    from_env = os.environ.get(PASSPHRASE_ENV)
    if from_env:
        return from_env
    return str(typer.prompt("Passphrase", hide_input=True, confirmation_prompt=confirm))


def _witness_keys(params: NetworkParams, keys: Sequence[str]) -> list[tuple[str, str]]:
    """Turn ``--relay-witness-key`` hex values into ``(address, key)`` pairs."""
    pairs: list[tuple[str, str]] = []
    for key_hex in keys:
        try:
            raw = bytes.fromhex(key_hex)
        except ValueError:
            _fail(f"--relay-witness-key {key_hex!r} is not valid hex", code=2)
        try:
            address = encode_address(raw, params)
        except ValueError as exc:
            _fail(f"--relay-witness-key {key_hex!r} is not a public key: {exc}", code=2)
        pairs.append((address, key_hex))
    return pairs


def _nonce_secret(value: str | None, path: Path | None) -> str | None:
    """Resolve ``--nonce-secret``/``--nonce-secret-file`` to one 32-byte hex secret."""
    if value is not None and path is not None:
        _fail("pass --nonce-secret or --nonce-secret-file, not both", code=2)
    if path is not None:
        try:
            value = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            _fail(f"cannot read {path}: {exc}")
    if value is None:
        return None
    try:
        raw = bytes.fromhex(value)
    except ValueError:
        _fail("the availability nonce secret is not valid hex", code=2)
    if len(raw) != 32:
        _fail(f"the availability nonce secret must be 32 bytes, not {len(raw)}", code=2)
    return raw.hex()


def _echo_json(payload: object) -> None:
    typer.echo(jsonlib.dumps(payload, sort_keys=True))


def _read_json(path: Path) -> object:
    """Read a JSON document, turning I/O and syntax errors into a clean exit."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        _fail(f"cannot read {path}: {exc}")
    try:
        return jsonlib.loads(text)
    except jsonlib.JSONDecodeError as exc:
        _fail(f"{path} is not valid JSON: {exc}")


def _emit_wallet(ctx: typer.Context, wallet: Wallet) -> None:
    if _options(ctx).json_output:
        _echo_json(
            {
                "address": wallet.address,
                "network": wallet.network,
                "public_key": wallet.public_key_hex,
                "scheme": wallet.scheme,
                "keyfile": str(wallet.path) if wallet.path is not None else None,
            }
        )
        return
    typer.echo(wallet.address)


def _wallet_scheme(scheme: str) -> str:
    """Validate a ``--scheme`` value before it reaches a keyfile."""
    if scheme not in SCHEMES:
        _fail(f"unknown scheme {scheme!r}; use one of: {', '.join(sorted(SCHEMES))}", code=2)
    return scheme


def _keyfile_kdf(scheme: str, kdf_n: int | None) -> KdfParams:
    """The scrypt parameters for a new keyfile.

    A hybrid keyfile defaults to the harder :data:`~sccp.crypto.wallet.PQ_KDF`
    (2**17) because one passphrase now protects an ML-DSA-65 key as well as the
    Ed25519 one; ``--kdf-n`` overrides it, which is what tests do to stay fast.
    """
    if kdf_n is not None:
        return KdfParams(n=kdf_n)
    return PQ_KDF if scheme == SCHEME_HYBRID else DEFAULT_KDF


def _classical_keypair(wallet: Wallet, purpose: str) -> Keypair:
    """The Ed25519 keypair of ``wallet``, refusing a hybrid keyfile.

    Match certificates, move and clock receipts and the relay's roster all carry
    raw Ed25519 keys, so a miner or witness identity is a version-0 address. A
    hybrid keyfile can hold and spend funds, but it cannot play or witness until
    those messages grow a hybrid form too.
    """
    if wallet.is_hybrid:
        _fail(
            f"a hybrid keyfile cannot {purpose}: match transcripts, clock receipts and "
            f"the relay roster are Ed25519-only. Use an Ed25519 keyfile "
            f"(sccp wallet new --scheme ed25519) for miner and witness commands"
        )
    return wallet.keypair


def _coins(units: int) -> str:
    """Format base units as a fixed-point coin amount; never a float."""
    return format_coins(units)


@contextmanager
def _local_node(ctx: typer.Context, network: str | None) -> Iterator[NodeService]:
    """Open the chain stored under ``--data-dir`` and close it on exit.

    Commands that build signed transactions need a ledger to read nonces and
    balances from. Rather than trusting a remote node's summary (or asking the
    node to sign for us), they read the same SQLite database the node writes,
    which is what an operator running both processes on one host already has.
    """
    params = _resolve_network(ctx, network)
    path = _options(ctx).data_dir / "chain.sqlite"
    if not path.is_file():
        _fail(
            f"no chain database at {path}; run `sccp --data-dir "
            f"{_options(ctx).data_dir} node run --network {params.network_id}` first"
        )
    storage = _run(lambda: Storage(path))
    service: NodeService | None = None
    try:
        service = _run(lambda: NodeService.open(params, storage=storage))
        yield service
    finally:
        if service is None:
            storage.close()
        else:
            service.close()


def _warn_empty_chain(path: Path) -> None:
    """Explain a zero balance that was read from a chain with no blocks.

    A genesis-only database is valid and opens cleanly, so nothing else about it
    looks wrong: the wallet reports every balance as zero while the node the
    user is watching reports real coins. Say why, and how to fix it. This is only
    reachable when no node answered, because a node is asked first.
    """
    typer.secho(
        f"note: {path} holds only the genesis block, so it knows no other balances, and "
        f"no node answered with one. Start the node that holds the coins and pass its "
        f"URL as --node: the wallet reads balances and nonces from that node, and "
        f"--data-dir is only the fallback for building a transfer offline.",
        err=True,
    )


NODE_READ_TIMEOUT = 5.0
"""Seconds a wallet command waits for one account read from a node."""


@dataclass(frozen=True, slots=True)
class _AccountSource:
    """The account a wallet command will spend from, and where it was read."""

    service: WalletService
    origin: str
    """The node URL, or the ``chain.sqlite`` path, the numbers came from."""
    chain_height: int | None = None
    """The local chain's tip height, or ``None`` when a node answered."""


def _node_account(node: str, address: str) -> Mapping[str, object] | None:
    """One account as ``node`` reports it, or ``None`` when it cannot answer.

    A wallet command reads the account from a node before falling back, so a
    node that is down must not look like a node that holds nothing.
    """
    client = NodeClient(node, timeout=NODE_READ_TIMEOUT, retries=1)
    try:
        return client.account(address)
    except (SccpError, ValueError):
        return None
    finally:
        client.close()


def _node_wallet(
    params: NetworkParams,
    document: Mapping[str, object],
    url: str,
    *,
    explicit: bool,
) -> WalletService | None:
    """A wallet built from a node's account document, or ``None`` to fall back.

    A node serving another network is a mistake worth stopping for when the user
    named it with ``--node``, and a reason to use ``--data-dir`` when the URL was
    only the default.
    """
    try:
        return WalletService.from_account_view(params, document, source=f"the node at {url}")
    except WalletServiceError as exc:
        if explicit:
            _fail(str(exc))
        return None


@contextmanager
def _account_source(
    ctx: typer.Context,
    params: NetworkParams,
    address: str,
    node: str | None,
) -> Iterator[_AccountSource]:
    """Read ``address`` from the node in ``--node`` when it answers, else locally.

    Whoever builds a transfer signs against one balance and one nonce, and a node
    checks them against its own chain: reading them from that node is the only
    way the two agree. Doing so also means a wallet on testnet needs no chain
    database and no sync. ``--data-dir`` stays as the fallback, so a transfer can
    still be built offline.
    """
    url = node or DEFAULT_NODE_URL
    document = _node_account(url, address)
    if document is not None:
        wallet = _node_wallet(params, document, url, explicit=node is not None)
        if wallet is not None:
            yield _AccountSource(service=wallet, origin=url)
            return
    if node is not None:
        _fail(
            f"cannot read {address} from the node at {node}: it did not answer. Pass the "
            f"URL of the node that serves the chain you are spending on, or drop --node "
            f"and pass --data-dir with that chain's database"
        )
    with _local_node(ctx, params.network_id) as local:
        path = _options(ctx).data_dir / "chain.sqlite"
        yield _AccountSource(
            service=WalletService(
                params,
                local.chain.state,
                chain=local.chain,
                source=f"the local chain in {path}",
            ),
            origin=str(path),
            chain_height=local.height,
        )


def _submit_transaction(node: str, tx: Transaction) -> Mapping[str, object]:
    """Submit one signed transaction to a node and return its receipt."""
    client = NodeClient(node)
    try:
        return _run(lambda: client.submit_transaction(tx))
    finally:
        client.close()


def _require_test_network(params: NetworkParams) -> None:
    """Refuse faucet commands on a network that has no faucet."""
    if params.network_id not in FAUCET_NETWORKS:
        usable = ", ".join(FAUCET_NETWORKS)
        _fail(f"{params.network_id} has no faucet; use one of: {usable}", code=2)


@wallet_app.command("new")
def wallet_new(
    ctx: typer.Context,
    out: Annotated[Path, typer.Option("--out", help="Path of the keyfile to write.")],
    network: Annotated[
        str | None,
        typer.Option("--network", help="Network the address is derived for."),
    ] = None,
    force: Annotated[
        bool,
        typer.Option("--force", help="Overwrite an existing keyfile."),
    ] = False,
    created_at: Annotated[
        int | None,
        typer.Option("--created-at", hidden=True, help="Fix created_at (tests)."),
    ] = None,
    scheme: Annotated[
        str,
        typer.Option(
            "--scheme",
            help="Key scheme: ed25519 (default, version-0 address) or ed25519-mldsa65.",
        ),
    ] = SCHEME_ED25519,
    kdf_n: Annotated[
        int | None,
        typer.Option(
            "--kdf-n", hidden=True, help="scrypt N (tests only; default 2**15, 2**17 hybrid)."
        ),
    ] = None,
) -> None:
    """Create an encrypted keyfile and print its address."""
    params = _resolve_network(ctx, network)
    resolved_scheme = _wallet_scheme(scheme)
    passphrase = _passphrase(ctx, confirm=True)
    wallet = _run(
        lambda: create_wallet(
            out,
            params,
            passphrase,
            scheme=resolved_scheme,
            kdf=_keyfile_kdf(resolved_scheme, kdf_n),
            created_at=created_at,
            force=force,
        )
    )
    _emit_wallet(ctx, wallet)


@wallet_app.command("address")
def wallet_address(
    ctx: typer.Context,
    key: Annotated[Path, typer.Option("--key", help="Keyfile to read.")],
    network: Annotated[
        str | None,
        typer.Option("--network", help="Require the keyfile to belong to this network."),
    ] = None,
) -> None:
    """Print the address stored in a keyfile; no passphrase is needed."""
    header = _run(lambda: read_header(key))
    _check_network(ctx, network, header.network)
    if _options(ctx).json_output:
        _echo_json(
            {
                "address": header.address,
                "network": header.network,
                "public_key": header.public_key,
                "scheme": header.scheme,
                "keyfile": str(key),
            }
        )
        return
    typer.echo(header.address)


@wallet_app.command("export-mnemonic")
def wallet_export_mnemonic(
    ctx: typer.Context,
    key: Annotated[Path, typer.Option("--key", help="Keyfile to decrypt.")],
    network: Annotated[
        str | None,
        typer.Option("--network", help="Require the keyfile to belong to this network."),
    ] = None,
) -> None:
    """Print the 24-word BIP-39 backup phrase for a keyfile."""
    header = _run(lambda: read_header(key))
    _check_network(ctx, network, header.network)
    passphrase = _passphrase(ctx, confirm=False)
    phrase = _run(lambda: export_mnemonic(key, passphrase))
    if _options(ctx).json_output:
        _echo_json(
            {
                "mnemonic": phrase,
                "address": header.address,
                "network": header.network,
                # A phrase is scheme-neutral; the keyfile says how to re-expand it.
                "scheme": header.scheme,
            }
        )
        return
    typer.echo(phrase)


@wallet_app.command("import-mnemonic")
def wallet_import_mnemonic(
    ctx: typer.Context,
    out: Annotated[Path, typer.Option("--out", help="Path of the keyfile to write.")],
    network: Annotated[
        str | None,
        typer.Option("--network", help="Network the address is derived for."),
    ] = None,
    force: Annotated[
        bool,
        typer.Option("--force", help="Overwrite an existing keyfile."),
    ] = False,
    created_at: Annotated[
        int | None,
        typer.Option("--created-at", hidden=True, help="Fix created_at (tests)."),
    ] = None,
    scheme: Annotated[
        str,
        typer.Option(
            "--scheme",
            help="Key scheme the phrase is expanded under: ed25519 or ed25519-mldsa65.",
        ),
    ] = SCHEME_ED25519,
    kdf_n: Annotated[
        int | None,
        typer.Option(
            "--kdf-n", hidden=True, help="scrypt N (tests only; default 2**15, 2**17 hybrid)."
        ),
    ] = None,
) -> None:
    """Recreate a keyfile from a 24-word backup phrase."""
    params = _resolve_network(ctx, network)
    resolved_scheme = _wallet_scheme(scheme)
    phrase = typer.prompt("Mnemonic (24 words)", hide_input=True)
    passphrase = _passphrase(ctx, confirm=True)
    wallet = _run(
        lambda: import_mnemonic(
            out,
            phrase,
            params,
            passphrase,
            scheme=resolved_scheme,
            kdf=_keyfile_kdf(resolved_scheme, kdf_n),
            created_at=created_at,
            force=force,
        )
    )
    _emit_wallet(ctx, wallet)


@wallet_app.command("balance")
def wallet_balance(
    ctx: typer.Context,
    key: Annotated[Path, typer.Option("--key", help="Keyfile whose address to inspect.")],
    node: Annotated[
        str | None,
        typer.Option("--node", help="Node to read the balance from, when it answers."),
    ] = None,
    network: Annotated[
        str | None,
        typer.Option("--network", help="Require the keyfile to belong to this network."),
    ] = None,
) -> None:
    """Print spendable, immature and bonded balances for the keyfile's address."""
    header = _run(lambda: read_header(key))
    _check_network(ctx, network, header.network)
    params = _resolve_network(ctx, network)
    with _account_source(ctx, params, header.address, node) as accounts:
        balance = _run(lambda: accounts.service.balance(header.address))
        empty_chain = accounts.chain_height == 0 and balance.total == 0
    if empty_chain:
        _warn_empty_chain(_options(ctx).data_dir / "chain.sqlite")
    payload = {
        "address": balance.address,
        "network": params.network_id,
        "spendable": balance.spendable,
        "immature": balance.immature,
        "bonded": balance.bonded,
        "total": balance.total,
        "source": accounts.origin,
    }
    if _options(ctx).json_output:
        _echo_json(payload)
        return
    typer.echo(
        f"{balance.address} {_coins(balance.total)} "
        f"(spendable {_coins(balance.spendable)}, bonded {_coins(balance.bonded)}, "
        f"immature {_coins(balance.immature)})"
    )


@wallet_app.command("send")
def wallet_send(
    ctx: typer.Context,
    key: Annotated[Path, typer.Option("--key", help="Keyfile to sign with.")],
    to: Annotated[str, typer.Option("--to", help="Recipient address.")],
    amount: Annotated[int, typer.Option("--amount", help="Amount to send, in whole coins.")],
    node: Annotated[
        str | None,
        typer.Option(
            "--node",
            help="Node that reports your balance and nonce and receives the transfer.",
        ),
    ] = None,
    fee: Annotated[
        int | None,
        typer.Option("--fee", help="Fee in base units (default: the network minimum)."),
    ] = None,
    network: Annotated[
        str | None,
        typer.Option("--network", help="Require the keyfile to belong to this network."),
    ] = None,
) -> None:
    """Sign a transfer against the node's account view and submit it to that node."""
    header = _run(lambda: read_header(key))
    _check_network(ctx, network, header.network)
    params = _resolve_network(ctx, network)
    passphrase = _passphrase(ctx, confirm=False)
    wallet = _run(lambda: load_wallet(key, passphrase))
    with _account_source(ctx, params, header.address, node) as accounts:
        try:
            tx = accounts.service.build_transfer(wallet.signer, to, coins_to_units(amount), fee=fee)
        except WalletServiceError as exc:
            if accounts.chain_height == 0:
                _warn_empty_chain(_options(ctx).data_dir / "chain.sqlite")
            _fail(str(exc))
    receipt = _submit_transaction(node or DEFAULT_NODE_URL, tx)
    payload = {
        "txid": txid(tx),
        "network": params.network_id,
        "type": str(tx.type),
        "from": tx.sender,
        "to": to,
        "amount": coins_to_units(amount),
        "fee": int(tx.fee),
        "nonce": tx.nonce,
        "accepted": receipt.get("accepted"),
        "source": accounts.origin,
    }
    if _options(ctx).json_output:
        _echo_json(payload)
        return
    typer.echo(f"sent {_coins(coins_to_units(amount))} to {to} (fee {_coins(int(tx.fee))})")
    typer.echo(f"txid: {payload['txid']}")


@wallet_app.command("history")
def wallet_history(
    ctx: typer.Context,
    key: Annotated[Path, typer.Option("--key", help="Keyfile whose address to inspect.")],
    network: Annotated[
        str | None,
        typer.Option("--network", help="Require the keyfile to belong to this network."),
    ] = None,
) -> None:
    """List every adopted-chain transaction that touched the keyfile's address."""
    header = _run(lambda: read_header(key))
    _check_network(ctx, network, header.network)
    params = _resolve_network(ctx, network)
    with _local_node(ctx, params.network_id) as service:
        entries = _run(
            lambda: WalletService(params, service.chain.state, chain=service.chain).history(
                header.address
            )
        )
    if _options(ctx).json_output:
        _echo_json(
            {
                "address": header.address,
                "network": params.network_id,
                "history": [
                    {
                        "txid": entry.txid,
                        "height": entry.height,
                        "type": str(entry.type),
                        "delta": entry.delta,
                        "fee": entry.fee,
                        "counterparty": entry.counterparty,
                    }
                    for entry in entries
                ],
            }
        )
        return
    if not entries:
        typer.echo(f"{header.address}: no transactions")
        return
    for entry in entries:
        counterparty = entry.counterparty or "-"
        typer.echo(
            f"{entry.height:>6} {entry.type!s:<12} {_coins(entry.delta):>16} "
            f"{counterparty} {entry.txid}"
        )


@verify_app.command("pgn")
def verify_pgn(
    ctx: typer.Context,
    file: Annotated[Path, typer.Argument(help="PGN file to verify.")],
    network: Annotated[
        str | None,
        typer.Option("--network", help="Network whose game domain the hash uses."),
    ] = None,
    require_win: Annotated[
        bool,
        typer.Option("--require-win", help="Apply the win-proof rules (decisive, min_plies)."),
    ] = False,
) -> None:
    """Replay a PGN, check its headers and print its canonical game hash."""
    params = _resolve_network(ctx, network)
    parsed = _run(lambda: validate_pgn_file(file, params=params, require_win=require_win))
    winner = None
    if parsed.winner is not None:
        winner = "white" if parsed.winner is GameResult.WHITE else "black"
    payload = {
        "game_hash": parsed.game_hash,
        "result": parsed.result.value,
        "termination": parsed.termination.value,
        "plies": parsed.plies,
        "winner": winner,
        "opening_id": int(parsed.tags["OpeningID"]),
        "match_id": parsed.tags["MatchID"],
    }
    if _options(ctx).json_output:
        _echo_json(payload)
        return
    typer.echo(f"game hash: {payload['game_hash']}")
    typer.echo(f"result: {payload['result']} ({payload['termination']}, {payload['plies']} plies)")
    typer.echo(f"match: {payload['match_id']} opening {payload['opening_id']}")


@verify_app.command("proof")
def verify_proof(
    ctx: typer.Context,
    file: Annotated[Path, typer.Argument(help="WinProof JSON file to verify.")],
    context: Annotated[
        Path,
        typer.Option(
            "--context",
            help="Public verification context JSON (beacon, roster, keys).",
        ),
    ],
    book_path: Annotated[
        Path | None,
        typer.Option("--book", help="Opening book JSON override for the context."),
    ] = None,
    network: Annotated[
        str | None,
        typer.Option("--network", help="Network to verify against."),
    ] = None,
) -> None:
    """Verify a win proof offline against a public context document."""
    params = _resolve_network(ctx, network)
    proof_document = _read_json(file)
    context_document = _read_json(context)
    book = None
    if book_path is not None:
        book = _run(lambda: load_book(params.book_version, book_path))
    try:
        proof = WinProof.model_validate(proof_document)
    except ValueError as exc:
        _fail(f"{file} is not a valid win proof: {exc}")
    if not isinstance(context_document, Mapping):
        _fail(f"{context} must contain a JSON object")
    verification_context: ProofContext = _run(
        lambda: context_from_document(context_document, params=params, book=book)
    )
    verified = _run(lambda: verify_win_proof(proof, verification_context))
    payload = {
        "proof_id": verified.proof.proof_id,
        "game_hash": verified.proof.game_hash,
        "match_id": verified.proof.match_id,
        "winner": verified.proof.winner,
        "winner_seat": verified.proof.winner_seat,
        "result": verified.proof.result,
        "termination": verified.proof.termination,
        "plies": verified.proof.plies,
    }
    if _options(ctx).json_output:
        _echo_json(payload)
        return
    typer.echo(f"proof: {payload['proof_id']}")
    typer.echo(f"game: {payload['game_hash']}")
    typer.echo(
        f"winner: {payload['winner']} ({payload['winner_seat']}, "
        f"{payload['result']} by {payload['termination']}, {payload['plies']} plies)"
    )


@matchmaker_app.command("run")
def matchmaker_run(
    ctx: typer.Context,
    config: Annotated[
        Path,
        typer.Option("--config", help="Relay config JSON: network, beacon, roster, engines."),
    ],
    host: Annotated[str, typer.Option("--host", help="Interface to bind.")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", help="Port to bind.")] = 8646,
    network: Annotated[
        str | None,
        typer.Option("--network", help="Require the config to be for this network."),
    ] = None,
    store: Annotated[
        Path | None,
        typer.Option("--store", help="JSON store that survives a relay restart."),
    ] = None,
    round_number: Annotated[
        int | None,
        typer.Option("--round", help="Open this round at startup."),
    ] = None,
    witness_key: Annotated[
        Path | None,
        typer.Option(
            "--witness-key",
            help="Keyfile whose key signs the relay's clock receipts (its witness identity).",
        ),
    ] = None,
    round_seconds: Annotated[
        float | None,
        typer.Option(
            "--round-seconds",
            help="Open the next round this many seconds after the last one (0 disables).",
        ),
    ] = None,
    sweep_seconds: Annotated[
        float,
        typer.Option(
            "--sweep-seconds",
            help="How often the relay checks clocks and the round schedule.",
        ),
    ] = 1.0,
    node: Annotated[
        str | None,
        typer.Option(
            "--node",
            help=(
                "Follow this node: move the relay onto each later epoch's beacon and "
                "roster as the chain opens it."
            ),
        ),
    ] = None,
    node_poll_seconds: Annotated[
        float,
        typer.Option("--node-poll-seconds", help="How often to ask the node for the tip height."),
    ] = 2.0,
    origin: Annotated[
        list[str] | None,
        typer.Option(
            "--origin",
            help=(
                "Browser origin allowed to call this relay (repeatable), e.g. "
                "--origin http://127.0.0.1:8645. Needed only when a web client plays."
            ),
        ),
    ] = None,
) -> None:
    """Serve the matchmaking relay over HTTP (uvicorn).

    The relay runs timed sessions: it starts both clocks when a match activates,
    charges moves against the mover, signs a receipt for every accepted move and
    flags a clock that reaches zero. ``--round-seconds`` adds the round schedule
    on top: round 0 opens at startup and the next round opens on the interval.
    With ``--node`` the relay also follows the chain: the config's epoch is only
    the starting point, and each later epoch the chain opens replaces the
    beacon and roster, so matches always reference the epoch whose proofs the
    chain accepts.
    """
    if sweep_seconds <= 0:
        _fail("--sweep-seconds must be positive", code=2)
    if node_poll_seconds <= 0:
        _fail("--node-poll-seconds must be positive", code=2)
    params = _resolve_network(ctx, network)
    document = _read_json(config)
    if not isinstance(document, Mapping):
        _fail(f"{config} must contain a JSON object")
    if round_number is not None:
        document = {**document, "open_round": round_number}
    if round_seconds is not None:
        document = {**document, "round_seconds": round_seconds}
    witness = None
    if witness_key is not None:
        passphrase = _passphrase(ctx, confirm=False)
        wallet = _run(lambda: load_wallet(witness_key, passphrase))
        _check_network(ctx, network, wallet.network)
        witness = _classical_keypair(wallet, "witness matches")
    state = _run(
        lambda: Matchmaker.from_document(
            document,
            params=params,
            store=store,
            witness_keypair=witness,
        )
    )
    typer.echo(
        f"matchmaker on {host}:{port} for {params.network_id} epoch {state.beacon.epoch} "
        f"({len(state.matches())} matches, round {state.current_round}, "
        f"witness {state.witness or 'none'})"
    )
    stop = Event()

    def service() -> None:  # pragma: no cover - runs in the uvicorn process
        while not stop.wait(sweep_seconds):
            try:
                flagged = state.sweep()
                for match_id in flagged:
                    typer.echo(f"clock flagged: {match_id}")
                opened = state.maybe_open_next_round(state.round_seconds)
                if opened:
                    typer.echo(f"opened round {state.current_round} with {len(opened)} matches")
            except SccpError as exc:
                typer.echo(f"relay sweep failed: {exc}", err=True)

    thread = Thread(target=service, name="relay-service", daemon=True)
    thread.start()
    follower: EpochFollower | None = None
    follower_thread: Thread | None = None
    if node is not None:
        follower = _run(
            lambda: EpochFollower(params=params, matchmaker=state, node=NodeClient(node))
        )

        def follow() -> None:  # pragma: no cover - runs in the relay process
            assert follower is not None
            while not stop.wait(node_poll_seconds):
                try:
                    advance = follower.step()
                except SccpError as exc:
                    typer.echo(f"epoch follower failed: {exc}", err=True)
                    continue
                if advance is not None:
                    typer.echo(f"relay moved to {advance.describe()}")

        follower_thread = Thread(target=follow, name="epoch-follower", daemon=True)
        follower_thread.start()
    import uvicorn

    try:
        uvicorn.run(
            create_matchmaker_app(state, origins=tuple(origin or ())),
            host=host,
            port=port,
        )
    finally:
        stop.set()
        thread.join(timeout=5)
        if follower_thread is not None:
            follower_thread.join(timeout=5)


@node_app.command("run")
def node_run(
    ctx: typer.Context,
    host: Annotated[str, typer.Option("--host", help="Interface to bind.")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", help="Port to bind.")] = 8645,
    network: Annotated[
        str | None,
        typer.Option("--network", help="Network id: mainnet, testnet or devnet."),
    ] = None,
    genesis: Annotated[
        Path | None,
        typer.Option("--genesis", help="Genesis document override (tests)."),
    ] = None,
    peer: Annotated[
        list[str] | None,
        typer.Option("--peer", help="Peer base URL to pull-sync from at startup (repeatable)."),
    ] = None,
    no_faucet: Annotated[
        bool,
        typer.Option("--no-faucet", help="Do not expose the testnet faucet endpoint."),
    ] = False,
    mine: Annotated[
        bool,
        typer.Option("--mine", help="Settle eligible proofs around a miner address."),
    ] = False,
    miner_address: Annotated[
        str | None,
        typer.Option("--miner-address", help="Address that receives coinbases for --mine."),
    ] = None,
    mine_poll_seconds: Annotated[
        float,
        typer.Option("--mine-poll-seconds", help="Seconds between --mine attempts."),
    ] = 1.0,
    mine_interval_seconds: Annotated[
        int,
        typer.Option(
            "--mine-interval-seconds",
            help="Minimum seconds between --mine blocks (0 mines as fast as it can).",
        ),
    ] = 0,
    mine_bootstrap: Annotated[
        bool,
        typer.Option(
            "--mine-bootstrap/--no-mine-bootstrap",
            help="Let --mine extend the empty bootstrap window (off for pre-built chains).",
        ),
    ] = True,
    web: Annotated[
        bool,
        typer.Option(
            "--web",
            help="Serve the built explorer (src/web/dist) at / next to the JSON API.",
        ),
    ] = False,
) -> None:
    """Serve the node API over HTTP, replaying the chain in ``--data-dir``."""
    params = _resolve_network(ctx, network)
    if mine:
        if miner_address is None:
            _fail("--mine needs --miner-address", code=2)
        if not validate_address(miner_address, params):
            _fail(f"{miner_address!r} is not a valid {params.network_id} address", code=2)
        if mine_poll_seconds <= 0:
            _fail("--mine-poll-seconds must be positive", code=2)
        if mine_interval_seconds < 0:
            _fail("--mine-interval-seconds must not be negative", code=2)
    data_dir = _options(ctx).data_dir
    data_dir.mkdir(parents=True, exist_ok=True)
    web_root = None
    if web:
        web_root = web_dist_root()
        if web_root is None:
            _fail(
                "no built explorer found; run `npm install && npm run build` in src/web "
                "or point SCCP_WEB_DIST at a directory holding index.html",
                code=2,
            )
    service = _run(
        lambda: NodeService.open(
            params,
            genesis_path=genesis,
            storage=Storage(data_dir / "chain.sqlite"),
        )
    )
    stop: Event | None = None
    thread: Thread | None = None
    try:
        if no_faucet:
            service.faucet = None
        _run(service.check_startup)
        reports = sync_peers(service, tuple(peer or ()))
        status = service.status()
        synced = {report.peer for report in reports}
        for url in peer or ():
            if url not in synced:
                typer.echo(f"warning: peer {url} did not answer; continuing without it", err=True)
        if _options(ctx).json_output:
            _echo_json(
                {
                    **status.model_dump(mode="json"),
                    "data_dir": str(data_dir),
                    "synced": [report.model_dump(mode="json") for report in reports],
                }
            )
        else:
            typer.echo(
                f"node on {host}:{port} for {params.network_id} height {status.height} "
                f"({status.mempool.transactions} txs, {status.mempool.proofs} proofs pending)"
            )
            typer.echo(f"tip: {status.tip_hash}")
            typer.echo(f"genesis: {status.genesis_hash}")
            for report in reports:
                typer.echo(
                    f"synced {report.peer}: +{report.adopted} blocks to height {report.height}"
                )
        if mine:
            assert miner_address is not None
            node_miner = NodeMiner(
                service,
                miner_address,
                clock=service.clock,
                mine_bootstrap=mine_bootstrap,
                min_interval_seconds=mine_interval_seconds,
            )
            stop = Event()
            thread = Thread(
                target=node_miner.run,
                kwargs={
                    "stop": stop,
                    "poll_seconds": mine_poll_seconds,
                    "on_block": lambda block: typer.echo(f"mined {proof_summary(block)}", err=True),
                },
                name="sccp-node-miner",
                daemon=True,
            )
            thread.start()
            typer.echo(f"mining proof blocks for {miner_address}")

        import uvicorn

        uvicorn.run(create_node_app(service, web_root=web_root), host=host, port=port)
    finally:
        if stop is not None:
            stop.set()
        if thread is not None:
            thread.join(timeout=5)
        service.close()


@devnet_app.command("up")
def devnet_up(
    ctx: typer.Context,
    miners: Annotated[
        int,
        typer.Option("--miners", help="How many miners to run (at least two)."),
    ] = 4,
    blocks: Annotated[
        int,
        typer.Option("--blocks", help="Chain height the devnet must reach."),
    ] = 10,
    genesis: Annotated[
        Path | None,
        typer.Option("--genesis", help="Genesis document override (tests)."),
    ] = None,
    engines: Annotated[
        str,
        typer.Option("--engines", help="Engine set to play with (only mock is supported)."),
    ] = "mock",
) -> None:
    """Run a full network in-process and report what it produced."""
    requested = _options(ctx).network
    if requested is not None and requested != "devnet":
        _fail(f"devnet up only runs the devnet network, not {requested!r}", code=2)
    if engines != "mock":
        _fail(f"devnet up only supports --engines mock, not {engines!r}", code=2)
    summary = _run(
        lambda: run_devnet(
            miners=miners,
            blocks=blocks,
            genesis_path=str(genesis) if genesis is not None else None,
        )
    )
    if _options(ctx).json_output:
        _echo_json(summary.as_dict())
        return
    typer.echo(f"devnet up: height {summary.height} after {summary.seconds:.3f}s")
    typer.echo(f"tip: {summary.tip_hash}")
    typer.echo(f"genesis: {summary.genesis_hash}")
    typer.echo(
        f"epochs: {', '.join(str(epoch) for epoch in summary.epochs) or 'none'}; "
        f"{len(summary.matches)} matches; {len(summary.proofs)} proofs mined"
    )
    for address, balance in summary.balances.items():
        typer.echo(f"  {address} {balance / 100_000_000:.8f}")


@miner_app.command("run")
def miner_run(
    ctx: typer.Context,
    key: Annotated[Path, typer.Option("--key", help="Keyfile the miner signs with.")],
    relay: Annotated[str, typer.Option("--relay", help="Matchmaking relay base URL.")] = (
        "http://127.0.0.1:8646"
    ),
    node: Annotated[
        str | None,
        typer.Option("--node", help="Node base URL that receives win proofs."),
    ] = None,
    engine_path: Annotated[
        Path | None,
        typer.Option("--engine-path", help="UCI binary; overrides the profile's pinned build."),
    ] = None,
    network: Annotated[
        str | None,
        typer.Option("--network", help="Require the keyfile to belong to this network."),
    ] = None,
    engine_profile: Annotated[
        str,
        typer.Option(
            "--engine-profile",
            help="Engine profile: stockfish-19, stockfish-18 or mock.",
        ),
    ] = "stockfish-19",
    nodes_per_move: Annotated[
        int | None,
        typer.Option("--nodes-per-move", help="Search limit per move (default: network policy)."),
    ] = None,
    depth: Annotated[
        int | None,
        typer.Option("--depth", help="Fixed search depth instead of a node limit."),
    ] = None,
    max_games_per_epoch: Annotated[
        int | None,
        typer.Option(
            "--max-games-per-epoch",
            help="Availability quota (default: the round count).",
        ),
    ] = None,
    nonce_secret: Annotated[
        str | None,
        typer.Option(
            "--nonce-secret",
            help=(
                "32-byte hex availability secret; makes the miner submit its own "
                "availability and reveal transactions every epoch."
            ),
        ),
    ] = None,
    nonce_secret_file: Annotated[
        Path | None,
        typer.Option(
            "--nonce-secret-file",
            help="File holding the availability secret (instead of --nonce-secret).",
        ),
    ] = None,
    relay_witness_key: Annotated[
        list[str] | None,
        typer.Option(
            "--relay-witness-key",
            help="Public key (hex) of the relay's clock witness, to verify its receipts.",
        ),
    ] = None,
    resign_after_plies: Annotated[
        int | None,
        typer.Option(
            "--resign-after-plies",
            help=(
                "Resign on our turn once a game reaches this many plies, so games "
                "end decisively instead of drawing (default: never resign)."
            ),
        ),
    ] = None,
    poll_seconds: Annotated[
        float,
        typer.Option("--poll-seconds", help="Seconds to wait between relay polls."),
    ] = 1.0,
    iterations: Annotated[
        int,
        typer.Option(
            "--iterations",
            help="Stop after N polls (0 keeps polling until interrupted).",
        ),
    ] = 0,
) -> None:
    """Play every match the relay assigns until interrupted."""
    if poll_seconds <= 0:
        _fail("--poll-seconds must be positive", code=2)
    if iterations < 0:
        _fail("--iterations cannot be negative", code=2)
    if resign_after_plies is not None and resign_after_plies < 1:
        _fail("--resign-after-plies must be at least 1 ply", code=2)
    secret = _nonce_secret(nonce_secret, nonce_secret_file)
    params = _resolve_network(ctx, network)
    passphrase = _passphrase(ctx, confirm=False)
    wallet = _run(lambda: load_wallet(key, passphrase))
    _check_network(ctx, network, wallet.network)
    engine = _run(lambda: open_engine(engine_profile, binary=engine_path))
    witness_keys = _witness_keys(params, relay_witness_key or [])
    config = MinerConfig.for_network(
        params,
        _classical_keypair(wallet, "run a miner"),
        relay_url=relay,
        node_url=node,
        nodes_per_move=nodes_per_move or params.nodes_per_move,
        depth=depth,
        max_games_per_epoch=max_games_per_epoch or params.rounds_per_epoch,
        nonce_secret=secret,
        witness_keys=witness_keys,
        resign_after_plies=resign_after_plies,
    )
    miner = EngineMiner(config, engine=engine)
    if not _options(ctx).json_output:
        typer.echo(
            f"miner {wallet.address} on {params.network_id} polling {relay} "
            f"with {engine.fingerprint}"
        )
        if secret is not None and node is None:
            typer.echo("--nonce-secret needs --node; the availability pipeline is off", err=True)
    polls = 0
    joined: list[str] = []
    moves: list[str] = []
    results: list[str] = []
    proofs: list[str] = []
    claims: list[str] = []
    resignations: list[str] = []
    refusals: list[str] = []
    started = time.monotonic()
    try:
        while True:
            tick = miner.run_once()
            polls += 1
            joined.extend(tick.joined)
            moves.extend(tick.moves)
            results.extend(tick.results)
            proofs.extend(tick.proofs)
            claims.extend(tick.claims)
            resignations.extend(tick.resignations)
            refusals.extend(tick.refusals)
            if iterations and polls >= iterations:
                break
            time.sleep(poll_seconds)
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        typer.echo("interrupted; shutting down", err=True)
    finally:
        miner.close()
    payload = {
        "address": wallet.address,
        "network": params.network_id,
        "relay": relay,
        "engine": engine.fingerprint,
        "polls": polls,
        "seconds": round(time.monotonic() - started, 3),
        "joined": joined,
        "moves": moves,
        "results": results,
        "proofs": proofs,
        "claims": claims,
        "resignations": resignations,
        "refusals": refusals,
    }
    if _options(ctx).json_output:
        _echo_json(payload)
        return
    typer.echo(
        f"stopped after {polls} polls ({payload['seconds']}s): "
        f"{len(joined)} joins, {len(moves)} moves, {len(results)} results, "
        f"{len(proofs)} proofs, {len(claims)} timeout claims, "
        f"{len(resignations)} resignations"
    )
    for reason in dict.fromkeys(refusals):
        typer.echo(f"  refused: {reason}")


@miner_app.command("register")
def miner_register(
    ctx: typer.Context,
    key: Annotated[Path, typer.Option("--key", help="Keyfile to register.")],
    endpoint: Annotated[str, typer.Option("--endpoint", help="Public endpoint the relay dials.")],
    bond: Annotated[int, typer.Option("--bond", help="Bond to lock, in whole coins.")] = 10,
    engine: Annotated[
        str,
        typer.Option("--engine", help="Engine fingerprint recorded on chain."),
    ] = "stockfish-19",
    node: Annotated[
        str | None,
        typer.Option("--node", help="Node base URL that receives the transaction."),
    ] = None,
    network: Annotated[
        str | None,
        typer.Option("--network", help="Require the keyfile to belong to this network."),
    ] = None,
) -> None:
    """Sign a registration transaction against the local chain and submit it."""
    passphrase = _passphrase(ctx, confirm=False)
    wallet = _run(lambda: load_wallet(key, passphrase))
    _check_network(ctx, network, wallet.network)
    params = wallet.network_params
    with _local_node(ctx, params.network_id) as service:
        tx = _run(
            lambda: WalletService(params, service.chain.state, chain=service.chain).build_register(
                _classical_keypair(wallet, "register a miner"),
                endpoint=endpoint,
                engine=engine,
                bond=coins_to_units(bond),
            )
        )
    receipt = _submit_transaction(node or DEFAULT_NODE_URL, tx)
    payload = {
        "txid": txid(tx),
        "network": params.network_id,
        "miner": wallet.address,
        "endpoint": endpoint,
        "engine": engine,
        "bond": coins_to_units(bond),
        "accepted": receipt.get("accepted"),
    }
    if _options(ctx).json_output:
        _echo_json(payload)
        return
    typer.echo(f"registered {wallet.address} with a {_coins(coins_to_units(bond))} bond")
    typer.echo(f"txid: {payload['txid']}")


@miner_app.command("availability")
def miner_availability(
    ctx: typer.Context,
    key: Annotated[Path, typer.Option("--key", help="Keyfile committing availability.")],
    max_games: Annotated[
        int, typer.Option("--max-games", help="Games the miner offers in the target epoch.")
    ] = 1,
    nonce_secret: Annotated[
        str | None,
        typer.Option("--nonce-secret", help="32-byte hex secret (default: freshly generated)."),
    ] = None,
    node: Annotated[
        str | None,
        typer.Option("--node", help="Node base URL that receives the transaction."),
    ] = None,
    network: Annotated[
        str | None,
        typer.Option("--network", help="Require the keyfile to belong to this network."),
    ] = None,
) -> None:
    """Commit availability for the epoch two ahead; keep the printed secret safe."""
    passphrase = _passphrase(ctx, confirm=False)
    wallet = _run(lambda: load_wallet(key, passphrase))
    _check_network(ctx, network, wallet.network)
    params = wallet.network_params
    secret = nonce_secret or secrets.token_hex(32)
    with _local_node(ctx, params.network_id) as service:
        wallet_service = WalletService(params, service.chain.state, chain=service.chain)
        tx = _run(
            lambda: wallet_service.build_availability(
                _classical_keypair(wallet, "commit availability"),
                nonce_secret=secret,
                max_games=max_games,
            )
        )
    receipt = _submit_transaction(node or DEFAULT_NODE_URL, tx)
    payload = {
        "txid": txid(tx),
        "network": params.network_id,
        "miner": wallet.address,
        "epoch": tx.payload.get("epoch"),
        "max_games": max_games,
        "commitment": tx.payload.get("commitment"),
        "nonce_secret": secret,
        "accepted": receipt.get("accepted"),
    }
    if _options(ctx).json_output:
        _echo_json(payload)
        return
    typer.echo(f"committed availability for epoch {payload['epoch']} ({max_games} games)")
    typer.echo(f"txid: {payload['txid']}")
    typer.echo(f"nonce secret (needed for the reveal): {secret}")


@miner_app.command("reveal")
def miner_reveal(
    ctx: typer.Context,
    key: Annotated[Path, typer.Option("--key", help="Keyfile revealing availability.")],
    nonce_secret: Annotated[
        str,
        typer.Option("--nonce-secret", help="The 32-byte hex secret used in the commitment."),
    ],
    node: Annotated[
        str | None,
        typer.Option("--node", help="Node base URL that receives the transaction."),
    ] = None,
    network: Annotated[
        str | None,
        typer.Option("--network", help="Require the keyfile to belong to this network."),
    ] = None,
) -> None:
    """Reveal the availability nonce so the epoch's beacon can be derived."""
    passphrase = _passphrase(ctx, confirm=False)
    wallet = _run(lambda: load_wallet(key, passphrase))
    _check_network(ctx, network, wallet.network)
    params = wallet.network_params
    with _local_node(ctx, params.network_id) as service:
        tx = _run(
            lambda: WalletService(params, service.chain.state, chain=service.chain).build_reveal(
                _classical_keypair(wallet, "reveal availability"),
                nonce_secret,
            )
        )
    receipt = _submit_transaction(node or DEFAULT_NODE_URL, tx)
    payload = {
        "txid": txid(tx),
        "network": params.network_id,
        "miner": wallet.address,
        "epoch": tx.payload.get("epoch"),
        "accepted": receipt.get("accepted"),
    }
    if _options(ctx).json_output:
        _echo_json(payload)
        return
    typer.echo(f"revealed for epoch {payload['epoch']}")
    typer.echo(f"txid: {payload['txid']}")


@chain_app.command("info")
def chain_info(
    ctx: typer.Context,
    network: Annotated[
        str | None,
        typer.Option("--network", help="Require the database to belong to this network."),
    ] = None,
) -> None:
    """Print height, tip, genesis and the current difficulty target."""
    with _local_node(ctx, network) as service:
        status = service.status()
        difficulty = service.difficulty()
    if _options(ctx).json_output:
        _echo_json(
            {
                **status.model_dump(mode="json"),
                "difficulty": difficulty.model_dump(mode="json"),
            }
        )
        return
    typer.echo(f"network: {status.network} (protocol {status.protocol_version})")
    typer.echo(f"height: {status.height}")
    typer.echo(f"tip: {status.tip_hash}")
    typer.echo(f"genesis: {status.genesis_hash}")
    typer.echo(
        f"target: wins_required {difficulty.wins_required}, seal_bits {difficulty.seal_bits} "
        f"(retarget every {difficulty.retarget_interval} blocks)"
    )


@chain_app.command("blocks")
def chain_blocks(
    ctx: typer.Context,
    limit: Annotated[int, typer.Option("--limit", help="How many recent blocks to print.")] = 10,
    network: Annotated[
        str | None,
        typer.Option("--network", help="Require the database to belong to this network."),
    ] = None,
) -> None:
    """List the most recent blocks on the adopted branch."""
    if limit < 1:
        _fail("--limit must be at least 1", code=2)
    with _local_node(ctx, network) as service:
        stored = service.chain.main_chain()[-limit:]
        rows = [
            {
                "height": item.height,
                "hash": item.block_hash,
                "timestamp": item.header.timestamp,
                "epoch": item.header.epoch,
                "transactions": len(item.block.transactions),
                "proofs": len(item.block.proofs),
            }
            for item in stored
        ]
    if _options(ctx).json_output:
        _echo_json(rows)
        return
    for row in rows:
        typer.echo(
            f"{row['height']:>6} {row['hash']} ts={row['timestamp']} epoch={row['epoch']} "
            f"{row['transactions']}tx {row['proofs']}proofs"
        )


@chain_app.command("account")
def chain_account(
    ctx: typer.Context,
    address: Annotated[str, typer.Argument(help="Address to inspect.")],
    network: Annotated[
        str | None,
        typer.Option("--network", help="Require the database to belong to this network."),
    ] = None,
) -> None:
    """Print balance, bond, nonce and registration for one address."""
    params = _resolve_network(ctx, network)
    with _local_node(ctx, params.network_id) as service:
        account = service.account(address)
    if _options(ctx).json_output:
        _echo_json(account.model_dump(mode="json"))
        return
    typer.echo(f"{account.address} ({params.network_id})")
    typer.echo(
        f"total {_coins(account.total)}: spendable {_coins(account.balance)}, "
        f"bonded {_coins(account.bonded)}, immature {_coins(account.immature)}"
    )
    typer.echo(f"nonce {account.nonce}")
    if account.registered_until_epoch is not None:
        typer.echo(
            f"registered until epoch {account.registered_until_epoch} "
            f"({account.engine}, {account.endpoint})"
        )
    else:
        typer.echo("not registered")


@chain_app.command("verify")
def chain_verify(
    ctx: typer.Context,
    network: Annotated[
        str | None,
        typer.Option("--network", help="Require the database to belong to this network."),
    ] = None,
) -> None:
    """Replay every stored block through full validation and report the tip."""
    with _local_node(ctx, network) as service:
        payload = {
            "network": service.params.network_id,
            "ok": True,
            "blocks": len(service.chain.main_chain()),
            "height": service.height,
            "tip_hash": service.tip_hash,
            "genesis_hash": service.genesis_hash,
        }
    if _options(ctx).json_output:
        _echo_json(payload)
        return
    typer.echo(
        f"ok: {payload['blocks']} blocks replay to height {payload['height']} "
        f"({payload['tip_hash']})"
    )


@faucet_app.command("send")
def faucet_send(
    ctx: typer.Context,
    to: Annotated[str, typer.Option("--to", help="Recipient address.")],
    amount: Annotated[int, typer.Option("--amount", help="Amount to send, in whole coins.")] = 1000,
    node: Annotated[
        str | None,
        typer.Option(
            "--node",
            help="Node that reports the faucet's balance and nonce and receives the transfer.",
        ),
    ] = None,
    network: Annotated[
        str | None,
        typer.Option("--network", help="Test network whose faucet sends the coins."),
    ] = None,
) -> None:
    """Send testnet coins from the network's documented faucet key."""
    params = _resolve_network(ctx, network)
    _require_test_network(params)
    keypair = _run(lambda: faucet_keypair(params.network_id))
    sender = encode_address(keypair.public_key_bytes, params)
    with _account_source(ctx, params, sender, node) as accounts:
        try:
            tx = _run(lambda: accounts.service.build_transfer(keypair, to, coins_to_units(amount)))
        except WalletServiceError as exc:
            if accounts.chain_height == 0:
                _warn_empty_chain(_options(ctx).data_dir / "chain.sqlite")
            _fail(str(exc))
    receipt = _submit_transaction(node or DEFAULT_NODE_URL, tx)
    payload = {
        "txid": txid(tx),
        "network": params.network_id,
        "from": sender,
        "to": to,
        "amount": coins_to_units(amount),
        "accepted": receipt.get("accepted"),
        "source": accounts.origin,
    }
    if _options(ctx).json_output:
        _echo_json(payload)
        return
    typer.echo(f"faucet sent {_coins(coins_to_units(amount))} to {to}")
    typer.echo(f"txid: {payload['txid']}")


if __name__ == "__main__":  # pragma: no cover - console-script parity
    app()
