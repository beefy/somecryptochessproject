"""``sccp`` command line interface.

The CLI exposes the wallet and keyfile tools, offline ``verify pgn`` and
``verify proof`` checks, the matchmaking relay, the node (``node run``), the
unattended miner (``miner run``, ``miner register``, ``miner availability``,
``miner reveal``), the read-only chain commands, the testnet faucet, the
one-command devnet, the on-chain DEX and the ETH/SCCP bridge. Every command is a
thin wrapper over a library call so the same behaviour is testable in-process.

Global options may be passed before or after the subcommand; an explicitly
passed ``--network`` always wins over ``SCCP_NETWORK``, and both win over the
default of ``testnet``.
"""

from __future__ import annotations

import json as jsonlib
import os
import secrets
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from threading import Event, Thread
from typing import Annotated, NoReturn, TypeVar

import httpx
import typer

from sccp import version as version_module
from sccp.bridge import (
    EthEnvironment,
    OrderError,
    bridge_environment,
    sccp_timelock_height,
    sign_order,
    sign_take,
)
from sccp.bridge.eth import (
    assert_environment,
    claim_eth,
    get_lock,
    lock_eth,
    refund_eth,
)
from sccp.chain import dex
from sccp.chain.genesis import faucet_keypair
from sccp.chain.state import HtlcRecord, reveal_deadline_tip
from sccp.chain.storage import Storage
from sccp.chain.tx import htlc_id, txid
from sccp.chess.engines import open_engine
from sccp.chess.openings import load_book
from sccp.chess.pgn import GameResult, validate_pgn_file
from sccp.chess.variants import is_chess960
from sccp.config import NetworkParams, coins_to_units, format_coins, get_network
from sccp.crypto.address import encode_address, validate_address
from sccp.crypto.pq import hybrid_sign
from sccp.crypto.wallet import (
    KdfParams,
    Wallet,
    create_wallet,
    export_mnemonic,
    import_mnemonic,
    load_wallet,
    read_header,
    read_mnemonic_file,
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
from sccp.serialization import Domain
from sccp.wallet.service import WalletService
from sccp.witness import WitnessService, create_witness_app

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
witness_app = typer.Typer(no_args_is_help=True, help="Run an independent clock witness.")
app.add_typer(witness_app, name="witness")
chain_app = typer.Typer(no_args_is_help=True, help="Read the chain stored in --data-dir.")
app.add_typer(chain_app, name="chain")
devnet_app = typer.Typer(no_args_is_help=True, help="Run a whole network in one process.")
app.add_typer(devnet_app, name="devnet")
faucet_app = typer.Typer(no_args_is_help=True, help="Send testnet coins from the public faucet.")
app.add_typer(faucet_app, name="faucet")

dex_app = typer.Typer(
    no_args_is_help=True,
    help="Create pools, provide liquidity and swap on the on-chain DEX.",
)
app.add_typer(dex_app, name="dex")

bridge_app = typer.Typer(
    no_args_is_help=True,
    help="Publish ETH/SCCP orders and lock, claim or refund bridge HTLCs.",
)
app.add_typer(bridge_app, name="bridge")


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


def _requested_network(ctx: typer.Context, network: str | None) -> str | None:
    """The network named by the command, the global flag, or ``SCCP_NETWORK``."""
    return network or _options(ctx).network or os.environ.get("SCCP_NETWORK")


def _resolve_network(ctx: typer.Context, network: str | None) -> NetworkParams:
    requested = _requested_network(ctx, network) or DEFAULT_NETWORK
    return _run(lambda: get_network(requested))


def _check_network(ctx: typer.Context, network: str | None, header_network: str) -> None:
    """Refuse to operate on a keyfile belonging to a different network."""
    explicit = _requested_network(ctx, network)
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


def _witness_keys(
    params: NetworkParams, keys: Sequence[str], files: Sequence[Path] = ()
) -> list[tuple[str, str]]:
    """Turn ``--relay-witness-key`` values into ``(address, key)`` pairs.

    Values are full hybrid public keys in hex, or the path of a file holding one
    (``--relay-witness-key-file``). The file form exists because a hybrid key is
    3 970 hex characters: the stack writes one file per witness and passes the
    path.
    """
    values = list(keys)
    for path in files:
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            _fail(f"cannot read {path}: {exc}")
        values.append(text)
    pairs: list[tuple[str, str]] = []
    for key_hex in values:
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


def _keyfile_kdf(kdf_n: int | None) -> KdfParams | None:
    """The scrypt parameters for a new keyfile.

    The default (:data:`~sccp.crypto.wallet.DEFAULT_KDF`, N=2**17) is the only
    production cost; ``--kdf-n`` exists so tests can stay fast.
    """
    return None if kdf_n is None else KdfParams(n=kdf_n)


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


def _node_document(
    node: str | None,
    path: str,
    document: Mapping[str, object] | None = None,
) -> Mapping[str, object]:
    """GET or POST one node document, with the node's error text on failure."""
    url = (node or DEFAULT_NODE_URL).rstrip("/")
    try:
        response = httpx.request(
            "POST" if document is not None else "GET",
            f"{url}{path}",
            json=document,
            timeout=10.0,
        )
    except httpx.HTTPError as exc:
        _fail(f"cannot reach the node at {url}: {exc}")
    if response.status_code >= 400:
        try:
            detail = response.json().get("detail")
        except (ValueError, AttributeError):
            detail = response.text
        _fail(f"{path} failed with {response.status_code}: {detail}")
    try:
        payload = response.json()
    except ValueError:
        _fail(f"{path} returned no JSON")
    if not isinstance(payload, Mapping):
        _fail(f"{path} did not return a JSON object")
    return payload


def _document_text(document: Mapping[str, object], key: str) -> str:
    """Read a non-empty string field from a node document."""
    value = document.get(key)
    if not isinstance(value, str) or value == "":
        _fail(f"the node document has no {key!r} string")
    return value


def _document_int(document: Mapping[str, object], key: str) -> int:
    """Read an integer field from a node document."""
    value = document.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        _fail(f"the node document has no {key!r} integer")
    return value


def _wallet_for(ctx: typer.Context, key: Path, network: str | None) -> Wallet:
    """Read a keyfile's header, check its network and unlock it."""
    header = _run(lambda: read_header(key))
    _check_network(ctx, network, header.network)
    passphrase = _passphrase(ctx, confirm=False)
    return _run(lambda: load_wallet(key, passphrase))


def _asset_symbol(params: NetworkParams, asset: str) -> str:
    """The printable symbol for an asset id, or a shortened id when unknown."""
    for variant in params.variants:
        if variant.asset == asset:
            return variant.symbol
    return f"{asset[:8]}…"


def _asset_id(params: NetworkParams, asset: str | None) -> str:
    """Resolve a CLI asset name to an id; native SCCP is the empty string."""
    if asset is None or asset.strip() == "" or asset.upper() == "SCCP":
        return dex.NATIVE_ASSET
    for variant in params.variants:
        if asset in (variant.asset, variant.name, variant.symbol):
            return variant.asset or dex.NATIVE_ASSET
    known = ", ".join(
        ["SCCP", *(variant.symbol for variant in params.variants if not variant.native)]
    )
    _fail(f"unknown asset {asset!r}; this network has: {known}")
    raise AssertionError("unreachable")


def _pool_document(node: str | None, pool_id: str) -> Mapping[str, object]:
    """Fetch one pool and check it names reserves this CLI can read."""
    document = _node_document(node, f"/v1/pools/{pool_id}")
    for key in ("reserve_a", "reserve_b", "total_shares", "fee_bps"):
        _document_int(document, key)
    return document


def _bridge_environment_for(
    params: NetworkParams,
) -> EthEnvironment:
    """Resolve the bridge environment, turning a ValueError into a CLI error."""
    try:
        return bridge_environment(params)
    except ValueError as exc:
        _fail(str(exc))
    raise AssertionError("unreachable")


def _eth_endpoint(
    ctx: typer.Context,
    *,
    node: str | None,
    network: str | None,
    rpc_url: str | None,
    contract: str | None,
    confirmations: int | None,
) -> tuple[EthEnvironment, str, str, int]:
    """Resolve and verify the RPC, contract and confirmation policy for an ETH action."""
    params = _resolve_network(ctx, network)
    environment = _bridge_environment_for(params)
    document = _node_document(node, "/v1/bridge")
    if _document_text(document, "network") != params.network_id:
        _fail("the node answers for a different network")
    document_rpc = document.get("eth_rpc_url")
    resolved_rpc = rpc_url or (document_rpc if isinstance(document_rpc, str) else None)
    resolved_rpc = resolved_rpc or environment.rpc_url
    if resolved_rpc is None:
        _fail(
            f"no {environment.chain_name} RPC is configured; set SCCP_ETH_RPC_URL or pass --rpc-url"
        )
    document_contract = document.get("eth_htlc_contract")
    resolved_contract = contract or (
        document_contract if isinstance(document_contract, str) else None
    )
    if resolved_contract is None:
        _fail(
            f"no {environment.chain_name} HTLC contract is configured; set "
            "SCCP_ETH_HTLC_CONTRACT on the node or pass --contract"
        )
    resolved_confirmations = (
        confirmations if confirmations is not None else _document_int(document, "eth_confirmations")
    )
    assert_environment(environment, url=resolved_rpc)
    return environment, resolved_rpc, resolved_contract, resolved_confirmations


def _transaction_summary(tx: Transaction, receipt: Mapping[str, object]) -> dict[str, object]:
    """The JSON fields every write command reports."""
    return {
        "txid": txid(tx),
        "network": tx.network,
        "type": str(tx.type),
        "from": tx.sender,
        "nonce": tx.nonce,
        "fee": int(tx.fee),
        "accepted": receipt.get("accepted"),
    }


def _report_transaction(
    ctx: typer.Context,
    tx: Transaction,
    receipt: Mapping[str, object],
    message: str,
) -> None:
    """Print one accepted transaction in JSON or human form."""
    payload = _transaction_summary(tx, receipt)
    if _options(ctx).json_output:
        _echo_json(payload)
        return
    typer.echo(message)
    typer.echo(f"txid: {payload['txid']}")


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
    kdf_n: Annotated[
        int | None,
        typer.Option("--kdf-n", hidden=True, help="scrypt N (tests only; default 2**17)."),
    ] = None,
) -> None:
    """Create an encrypted hybrid keyfile and print its address."""
    params = _resolve_network(ctx, network)
    passphrase = _passphrase(ctx, confirm=True)
    wallet = _run(
        lambda: create_wallet(
            out,
            params,
            passphrase,
            kdf=_keyfile_kdf(kdf_n),
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
    mnemonic_file: Annotated[
        Path | None,
        typer.Option(
            "--mnemonic-file",
            help=(
                "File holding the 24-word phrase (mode 0600, whitespace-insensitive); "
                "skips the hidden prompt so the command works without a terminal."
            ),
        ),
    ] = None,
    force: Annotated[
        bool,
        typer.Option("--force", help="Overwrite an existing keyfile."),
    ] = False,
    created_at: Annotated[
        int | None,
        typer.Option("--created-at", hidden=True, help="Fix created_at (tests)."),
    ] = None,
    kdf_n: Annotated[
        int | None,
        typer.Option("--kdf-n", hidden=True, help="scrypt N (tests only; default 2**17)."),
    ] = None,
) -> None:
    """Recreate a keyfile from a 24-word backup phrase."""
    params = _resolve_network(ctx, network)
    if mnemonic_file is not None:
        phrase = _run(lambda: read_mnemonic_file(mnemonic_file))
    else:
        phrase = typer.prompt("Mnemonic (24 words)", hide_input=True)
    passphrase = _passphrase(ctx, confirm=True)
    wallet = _run(
        lambda: import_mnemonic(
            out,
            phrase,
            params,
            passphrase,
            kdf=_keyfile_kdf(kdf_n),
            created_at=created_at,
            force=force,
        )
    )
    _emit_wallet(ctx, wallet)


@wallet_app.command("sign-result")
def wallet_sign_result(
    ctx: typer.Context,
    key: Annotated[Path, typer.Option("--key", help="Keyfile with the seat's hybrid key.")],
    payload: Annotated[
        str,
        typer.Option(
            "--payload",
            help="Hex of the canonical result payload the browser tab printed.",
        ),
    ],
) -> None:
    """Hybrid-sign a result payload produced in the browser.

    The web tab plays moves with the Ed25519 half of the seat's key but cannot
    produce an ML-DSA-65 signature, so it prints the canonical payload and this
    command signs it with the same keyfile. The output is the 6748-hex signature
    ``POST /v1/matches/{id}/result`` expects.
    """
    passphrase = _passphrase(ctx, confirm=False)
    wallet = _run(lambda: load_wallet(key, passphrase))

    def sign() -> str:
        try:
            raw = bytes.fromhex(payload.strip())
        except ValueError:
            _fail("--payload must be lowercase hex of the canonical JSON payload", code=2)
        digest = sha256(Domain.RESULT.encode("utf-8") + b"\x00" + raw).digest()
        return hybrid_sign(wallet.signer, digest).hex()

    signature = _run(sign)
    if _options(ctx).json_output:
        _echo_json({"address": wallet.address, "payload": payload.strip(), "signature": signature})
        return
    typer.echo(signature)


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
        account = _run(lambda: accounts.service.account(header.address))
        assets = {asset: units for asset, units in account.assets.items() if units > 0}
        immature_assets = {
            asset: units for asset, units in account.immature_assets.items() if units > 0
        }
        empty_chain = accounts.chain_height == 0 and balance.total == 0 and not assets
    if empty_chain:
        _warn_empty_chain(_options(ctx).data_dir / "chain.sqlite")
    payload = {
        "address": balance.address,
        "network": params.network_id,
        "spendable": balance.spendable,
        "immature": balance.immature,
        "bonded": balance.bonded,
        "total": balance.total,
        "assets": dict(sorted(assets.items())),
        "immature_assets": dict(sorted(immature_assets.items())),
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
    for asset in sorted(set(assets) | set(immature_assets)):
        typer.echo(
            f"  {_asset_symbol(params, asset)} spendable {_coins(assets.get(asset, 0))}, "
            f"immature {_coins(immature_assets.get(asset, 0))}"
        )


@wallet_app.command("send")
def wallet_send(
    ctx: typer.Context,
    key: Annotated[Path, typer.Option("--key", help="Keyfile to sign with.")],
    to: Annotated[str, typer.Option("--to", help="Recipient address.")],
    amount: Annotated[int, typer.Option("--amount", help="Amount to send, in whole coins.")],
    asset: Annotated[
        str | None,
        typer.Option(
            "--asset",
            help="Asset id, variant name or symbol (default: native SCCP).",
        ),
    ] = None,
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
            tx = accounts.service.build_transfer(
                wallet.signer, to, coins_to_units(amount), fee=fee, asset=asset
            )
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
        "asset": tx.payload.get("asset", "SCCP"),
        "fee": int(tx.fee),
        "nonce": tx.nonce,
        "accepted": receipt.get("accepted"),
        "source": accounts.origin,
    }
    if _options(ctx).json_output:
        _echo_json(payload)
        return
    asset_label = f" {asset}" if asset is not None else ""
    typer.echo(
        f"sent {_coins(coins_to_units(amount))}{asset_label} to {to} (fee {_coins(int(tx.fee))})"
    )
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
        witness = wallet.signer
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
    peer_poll_seconds: Annotated[
        float,
        typer.Option(
            "--peer-poll-seconds",
            help=(
                "Keep pulling from --peer every N seconds (0 syncs once at startup). "
                "A read replica needs this; a startup sync alone goes stale."
            ),
        ),
    ] = 0.0,
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
    if peer_poll_seconds < 0:
        _fail("--peer-poll-seconds cannot be negative", code=2)
    if peer_poll_seconds > 0 and not peer:
        _fail("--peer-poll-seconds needs --peer", code=2)
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
    peer_stop: Event | None = None
    peer_thread: Thread | None = None
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
        if peer and peer_poll_seconds > 0:
            peer_stop = Event()

            def replicate() -> None:  # pragma: no cover - runs in the node process
                while not peer_stop.wait(peer_poll_seconds):
                    try:
                        pulled = sync_peers(service, tuple(peer))
                    except SccpError as exc:
                        typer.echo(f"peer sync failed: {exc}", err=True)
                        continue
                    for report in pulled:
                        if report.adopted:
                            typer.echo(
                                f"replicated {report.peer}: +{report.adopted} blocks "
                                f"to height {report.height}",
                                err=True,
                            )

            peer_thread = Thread(target=replicate, name="sccp-peer-sync", daemon=True)
            peer_thread.start()
            typer.echo(f"replicating from {', '.join(peer)} every {peer_poll_seconds:g}s")
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
        if peer_stop is not None:
            peer_stop.set()
        if peer_thread is not None:
            peer_thread.join(timeout=5)
        service.close()


@witness_app.command("run")
def witness_run(
    ctx: typer.Context,
    key: Annotated[Path, typer.Option("--key", help="Keyfile the witness signs with.")],
    relay: Annotated[
        str,
        typer.Option("--relay", help="Matchmaking relay to observe."),
    ] = "http://127.0.0.1:8646",
    host: Annotated[str, typer.Option("--host", help="Interface to bind.")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", help="Port to bind.")] = 8647,
    network: Annotated[
        str | None,
        typer.Option("--network", help="Require the keyfile to belong to this network."),
    ] = None,
    relay_witness_key: Annotated[
        list[str] | None,
        typer.Option(
            "--relay-witness-key",
            help="Public key (hex) of a witness whose receipts to verify (repeatable).",
        ),
    ] = None,
    relay_witness_key_file: Annotated[
        list[Path] | None,
        typer.Option(
            "--relay-witness-key-file",
            help="File holding a witness's hybrid public key (repeatable).",
        ),
    ] = None,
    poll_seconds: Annotated[
        float,
        typer.Option("--poll-seconds", help="Seconds between relay polls."),
    ] = 2.0,
) -> None:
    """Observe a relay and publish evidence signed with this key.

    The witness is read-only towards the relay: it fetches every open match's
    receipts, checks the clock arithmetic with the protocol's own verifier, and
    co-signs what it observed. Its bundle is served on
    ``GET /v1/matches/{id}/receipts``, so ``sccp miner run --witness-url`` can
    settle a timeout even when the relay cannot produce the evidence itself.

    ``--relay-witness-key``/``--relay-witness-key-file`` name the keys whose
    signatures the witness checks before adding its own. A receipt carrying no
    signature at all is signed anyway and reported: that is a relay running
    without ``--witness-key``, which attests to nothing on its own.
    """
    if poll_seconds <= 0:
        _fail("--poll-seconds must be positive", code=2)
    params = _resolve_network(ctx, network)
    passphrase = _passphrase(ctx, confirm=False)
    wallet = _run(lambda: load_wallet(key, passphrase))
    _check_network(ctx, network, wallet.network)
    trusted = dict(_witness_keys(params, relay_witness_key or [], relay_witness_key_file or []))
    service = WitnessService(params, wallet.signer, relay_url=relay, relay_keys=trusted)
    state = service.status()
    typer.echo(
        f"witness {state['address']} on {params.network_id} watching {relay} "
        f"({len(trusted)} trusted key(s), quorum {params.clock_witness_quorum})"
    )
    stop = Event()

    def follow() -> None:  # pragma: no cover - runs in the uvicorn process
        while not stop.wait(poll_seconds):
            for match_id in service.poll():
                typer.echo(f"signed receipts for {match_id}", err=True)

    service.poll()
    thread = Thread(target=follow, name="witness-poll", daemon=True)
    thread.start()
    import uvicorn

    try:
        uvicorn.run(create_witness_app(service), host=host, port=port)
    finally:
        stop.set()
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
    variant: Annotated[
        str,
        typer.Option(
            "--variant",
            help="Chess variant this miner plays: standard or chess960.",
        ),
    ] = "standard",
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
    relay_witness_key_file: Annotated[
        list[Path] | None,
        typer.Option(
            "--relay-witness-key-file",
            help="File holding a clock witness's hybrid public key (repeatable).",
        ),
    ] = None,
    witness_url: Annotated[
        list[str] | None,
        typer.Option(
            "--witness-url",
            help=(
                "Independent witness base URL to fall back on for a timeout claim's "
                "evidence (repeatable)."
            ),
        ),
    ] = None,
    move_delay_seconds: Annotated[
        float,
        typer.Option(
            "--move-delay-seconds",
            help=(
                "Wait this long before searching on our turn, so the seat can be "
                "flagged (default: 0, an honest pace)."
            ),
        ),
    ] = 0.0,
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
    if move_delay_seconds < 0:
        _fail("--move-delay-seconds cannot be negative", code=2)
    secret = _nonce_secret(nonce_secret, nonce_secret_file)
    params = _resolve_network(ctx, network)
    _run(lambda: params.variant(variant))
    passphrase = _passphrase(ctx, confirm=False)
    wallet = _run(lambda: load_wallet(key, passphrase))
    _check_network(ctx, network, wallet.network)
    engine = _run(
        lambda: open_engine(
            engine_profile,
            binary=engine_path,
            chess960=is_chess960(variant),
        )
    )
    witness_keys = _witness_keys(params, relay_witness_key or [], relay_witness_key_file or [])
    config = MinerConfig.for_network(
        params,
        wallet.signer,
        relay_url=relay,
        node_url=node,
        nodes_per_move=nodes_per_move or params.nodes_per_move,
        depth=depth,
        max_games_per_epoch=max_games_per_epoch or params.rounds_per_epoch,
        nonce_secret=secret,
        witness_keys=witness_keys,
        witness_urls=tuple(witness_url or ()),
        resign_after_plies=resign_after_plies,
        variant=variant,
        move_delay_seconds=move_delay_seconds,
    )
    miner = EngineMiner(config, engine=engine)
    if not _options(ctx).json_output:
        typer.echo(
            f"miner {wallet.address} on {params.network_id} variant {variant} "
            f"polling {relay} with {engine.fingerprint}"
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
        "variant": variant,
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
    variant: Annotated[
        str,
        typer.Option(
            "--variant",
            help="Chess variant this registration plays: standard or chess960.",
        ),
    ] = "standard",
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
    _run(lambda: params.variant(variant))
    with _local_node(ctx, params.network_id) as service:
        tx = _run(
            lambda: WalletService(params, service.chain.state, chain=service.chain).build_register(
                wallet.signer,
                endpoint=endpoint,
                engine=engine,
                bond=coins_to_units(bond),
                variant=variant,
            )
        )
    receipt = _submit_transaction(node or DEFAULT_NODE_URL, tx)
    payload = {
        "txid": txid(tx),
        "network": params.network_id,
        "miner": wallet.address,
        "endpoint": endpoint,
        "engine": engine,
        "variant": variant,
        "bond": coins_to_units(bond),
        "accepted": receipt.get("accepted"),
    }
    if _options(ctx).json_output:
        _echo_json(payload)
        return
    typer.echo(
        f"registered {wallet.address} for {variant} with a {_coins(coins_to_units(bond))} bond"
    )
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
    """Commit availability for the epoch two ahead; print the secret and the reveal deadline."""
    passphrase = _passphrase(ctx, confirm=False)
    wallet = _run(lambda: load_wallet(key, passphrase))
    _check_network(ctx, network, wallet.network)
    params = wallet.network_params
    secret = nonce_secret or secrets.token_hex(32)
    with _local_node(ctx, params.network_id) as service:
        wallet_service = WalletService(params, service.chain.state, chain=service.chain)
        tx = _run(
            lambda: wallet_service.build_availability(
                wallet.signer,
                nonce_secret=secret,
                max_games=max_games,
            )
        )
    receipt = _submit_transaction(node or DEFAULT_NODE_URL, tx)
    target = tx.payload.get("epoch")
    if not isinstance(target, int):
        _fail("the availability transaction carries no target epoch")
    deadline = reveal_deadline_tip(target, params)
    payload = {
        "txid": txid(tx),
        "network": params.network_id,
        "miner": wallet.address,
        "epoch": target,
        "max_games": max_games,
        "commitment": tx.payload.get("commitment"),
        "nonce_secret": secret,
        "reveal_by_tip": deadline,
        "accepted": receipt.get("accepted"),
    }
    if _options(ctx).json_output:
        _echo_json(payload)
        return
    typer.echo(f"committed availability for epoch {target} ({max_games} games)")
    typer.echo(f"txid: {payload['txid']}")
    typer.echo(f"nonce secret (needed for the reveal): {secret}")
    typer.echo(
        f"reveal by tip {deadline} (block {deadline + 1} is the last of "
        f"epoch {target - 1}), or the commitment is void"
    )


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
                wallet.signer,
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


@dex_app.command("pools")
def dex_pools(
    ctx: typer.Context,
    node: Annotated[str | None, typer.Option("--node", help="Node that serves the pools.")] = None,
) -> None:
    """List every constant-product pool on the node's chain."""
    document = _node_document(node, "/v1/pools")
    if _options(ctx).json_output:
        _echo_json(document)
        return
    rows = document.get("pools")
    if not isinstance(rows, list) or not rows:
        typer.echo("no pools on this chain")
        return
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        typer.echo(
            f"{row.get('pool_id')} {row.get('asset_a') or 'SCCP'}/"
            f"{row.get('asset_b') or 'SCCP'} reserves "
            f"{row.get('reserve_a')}/{row.get('reserve_b')} "
            f"shares {row.get('total_shares')}"
        )


@dex_app.command("pool")
def dex_pool(
    ctx: typer.Context,
    pool_id: Annotated[str, typer.Option("--pool", help="Pool id to inspect.")],
    node: Annotated[str | None, typer.Option("--node", help="Node that serves the pool.")] = None,
) -> None:
    """Show one pool's reserves, fee and LP share ledger."""
    document = _pool_document(node, pool_id)
    if _options(ctx).json_output:
        _echo_json(document)
        return
    typer.echo(f"{pool_id} {document.get('asset_a') or 'SCCP'}/{document.get('asset_b') or 'SCCP'}")
    typer.echo(
        f"reserves {document['reserve_a']}/{document['reserve_b']} "
        f"shares {document['total_shares']} fee {document['fee_bps']} bps"
    )
    shares = document.get("shares")
    if isinstance(shares, Mapping):
        for address, amount in shares.items():
            typer.echo(f"  {address} {amount}")


@dex_app.command("quote")
def dex_quote(
    ctx: typer.Context,
    pool_id: Annotated[str, typer.Option("--pool", help="Pool id to quote.")],
    amount_in: Annotated[int, typer.Option("--amount-in", help="Input amount, in whole coins.")],
    asset_in: Annotated[
        str | None,
        typer.Option("--asset-in", help="Asset id, variant name or symbol (omit for SCCP)."),
    ] = None,
    slippage_bps: Annotated[
        int,
        typer.Option("--slippage-bps", help="Slippage used for the printed minimum."),
    ] = 50,
    node: Annotated[str | None, typer.Option("--node", help="Node that serves the pool.")] = None,
) -> None:
    """Quote a constant-product swap before signing it."""
    if amount_in <= 0:
        _fail("--amount-in must be positive", code=2)
    if not 0 <= slippage_bps < 10_000:
        _fail("--slippage-bps must be between 0 and 9999", code=2)
    params = _resolve_network(ctx, None)
    pool = _pool_document(node, pool_id)
    resolved = _asset_id(params, asset_in)
    left = pool.get("asset_a") or dex.NATIVE_ASSET
    right = pool.get("asset_b") or dex.NATIVE_ASSET
    if resolved not in (left, right):
        _fail(f"pool {pool_id} does not trade {asset_in or 'SCCP'}")
    native_in = resolved == left
    reserve_in = _document_int(pool, "reserve_a" if native_in else "reserve_b")
    reserve_out = _document_int(pool, "reserve_b" if native_in else "reserve_a")
    fee_bps = _document_int(pool, "fee_bps")
    units_in = coins_to_units(amount_in)
    units_out = dex.swap_output(reserve_in, reserve_out, units_in, fee_bps)
    minimum = units_out * (10_000 - slippage_bps) // 10_000
    if _options(ctx).json_output:
        _echo_json(
            {
                "pool_id": pool_id,
                "asset_in": resolved,
                "amount_in": units_in,
                "amount_out": units_out,
                "min_amount_out": minimum,
                "fee_bps": fee_bps,
            }
        )
        return
    typer.echo(f"in   {_coins(units_in)} at {asset_in or 'SCCP'}")
    typer.echo(f"out  {_coins(units_out)}")
    typer.echo(f"min  {_coins(minimum)} ({slippage_bps / 100:.2f}% slippage)")


@dex_app.command("create-pool")
def dex_create_pool(
    ctx: typer.Context,
    key: Annotated[Path, typer.Option("--key", help="LP keyfile that funds both sides.")],
    amount_a: Annotated[int, typer.Option("--amount-a", help="First side, in whole coins.")],
    amount_b: Annotated[int, typer.Option("--amount-b", help="Second side, in whole coins.")],
    asset_a: Annotated[
        str | None,
        typer.Option("--asset-a", help="First asset id/name/symbol (omit for SCCP)."),
    ] = None,
    asset_b: Annotated[
        str | None,
        typer.Option("--asset-b", help="Second asset id/name/symbol (omit for SCCP)."),
    ] = None,
    fee: Annotated[int | None, typer.Option("--fee", help="Fee in base units.")] = None,
    node: Annotated[
        str | None, typer.Option("--node", help="Node that receives the transaction.")
    ] = None,
    network: Annotated[str | None, typer.Option("--network", help="Require this network.")] = None,
) -> None:
    """Create a pool and deposit the first two reserves."""
    params = _resolve_network(ctx, network)
    wallet = _wallet_for(ctx, key, network)
    with _account_source(ctx, params, wallet.address, node) as accounts:
        tx = _run(
            lambda: accounts.service.build_create_pool(
                wallet.signer,
                asset_a=asset_a,
                amount_a=coins_to_units(amount_a),
                asset_b=asset_b,
                amount_b=coins_to_units(amount_b),
                fee=fee,
            )
        )
    receipt = _submit_transaction(node or DEFAULT_NODE_URL, tx)
    normalized_a, _, normalized_b, _ = dex.normalize_pair(
        _asset_id(params, asset_a), 1, _asset_id(params, asset_b), 1
    )
    payload = {
        **_transaction_summary(tx, receipt),
        "pool_id": dex.pool_id(normalized_a, normalized_b),
    }
    if _options(ctx).json_output:
        _echo_json(payload)
        return
    typer.echo(f"created pool {payload['pool_id']}")
    typer.echo(f"txid: {payload['txid']}")


@dex_app.command("add-liquidity")
def dex_add_liquidity(
    ctx: typer.Context,
    key: Annotated[Path, typer.Option("--key", help="LP keyfile that funds the deposit.")],
    pool_id: Annotated[str, typer.Option("--pool", help="Pool id to add to.")],
    amount_a: Annotated[int, typer.Option("--amount-a", help="Pool asset_a, in whole coins.")],
    amount_b: Annotated[int, typer.Option("--amount-b", help="Pool asset_b, in whole coins.")],
    fee: Annotated[int | None, typer.Option("--fee", help="Fee in base units.")] = None,
    node: Annotated[
        str | None, typer.Option("--node", help="Node that receives the transaction.")
    ] = None,
    network: Annotated[str | None, typer.Option("--network", help="Require this network.")] = None,
) -> None:
    """Add a proportional two-sided deposit to an existing pool."""
    params = _resolve_network(ctx, network)
    wallet = _wallet_for(ctx, key, network)
    with _account_source(ctx, params, wallet.address, node) as accounts:
        tx = _run(
            lambda: accounts.service.build_add_liquidity(
                wallet.signer,
                pool_id=pool_id,
                amount_a=coins_to_units(amount_a),
                amount_b=coins_to_units(amount_b),
                fee=fee,
            )
        )
    receipt = _submit_transaction(node or DEFAULT_NODE_URL, tx)
    _report_transaction(ctx, tx, receipt, "added liquidity")


@dex_app.command("remove-liquidity")
def dex_remove_liquidity(
    ctx: typer.Context,
    key: Annotated[Path, typer.Option("--key", help="LP keyfile that owns the shares.")],
    pool_id: Annotated[str, typer.Option("--pool", help="Pool id to redeem.")],
    shares: Annotated[int, typer.Option("--shares", help="Pool shares to redeem.")],
    fee: Annotated[int | None, typer.Option("--fee", help="Fee in base units.")] = None,
    node: Annotated[
        str | None, typer.Option("--node", help="Node that receives the transaction.")
    ] = None,
    network: Annotated[str | None, typer.Option("--network", help="Require this network.")] = None,
) -> None:
    """Redeem pool shares for both reserves."""
    params = _resolve_network(ctx, network)
    wallet = _wallet_for(ctx, key, network)
    with _account_source(ctx, params, wallet.address, node) as accounts:
        tx = _run(
            lambda: accounts.service.build_remove_liquidity(
                wallet.signer,
                pool_id=pool_id,
                shares=shares,
                fee=fee,
            )
        )
    receipt = _submit_transaction(node or DEFAULT_NODE_URL, tx)
    _report_transaction(ctx, tx, receipt, "removed liquidity")


@dex_app.command("swap")
def dex_swap(
    ctx: typer.Context,
    key: Annotated[Path, typer.Option("--key", help="Keyfile that pays the swap input.")],
    pool_id: Annotated[str, typer.Option("--pool", help="Pool id to swap through.")],
    amount_in: Annotated[int, typer.Option("--amount-in", help="Input amount, in whole coins.")],
    asset_in: Annotated[
        str | None,
        typer.Option("--asset-in", help="Asset id, variant name or symbol (omit for SCCP)."),
    ] = None,
    min_out: Annotated[
        int | None,
        typer.Option(
            "--min-out", help="Minimum output, in whole coins (default: quote minus slippage)."
        ),
    ] = None,
    slippage_bps: Annotated[
        int,
        typer.Option("--slippage-bps", help="Slippage used when --min-out is omitted."),
    ] = 50,
    fee: Annotated[int | None, typer.Option("--fee", help="Fee in base units.")] = None,
    node: Annotated[
        str | None, typer.Option("--node", help="Node that receives the transaction.")
    ] = None,
    network: Annotated[str | None, typer.Option("--network", help="Require this network.")] = None,
) -> None:
    """Swap one pool side for the other, with a minimum output in the signature."""
    if min_out is not None and min_out < 0:
        _fail("--min-out cannot be negative", code=2)
    if not 0 <= slippage_bps < 10_000:
        _fail("--slippage-bps must be between 0 and 9999", code=2)
    params = _resolve_network(ctx, network)
    pool = _pool_document(node, pool_id)
    resolved = _asset_id(params, asset_in)
    left = pool.get("asset_a") or dex.NATIVE_ASSET
    right = pool.get("asset_b") or dex.NATIVE_ASSET
    if resolved not in (left, right):
        _fail(f"pool {pool_id} does not trade {asset_in or 'SCCP'}")
    native_in = resolved == left
    reserve_in = _document_int(pool, "reserve_a" if native_in else "reserve_b")
    reserve_out = _document_int(pool, "reserve_b" if native_in else "reserve_a")
    quote = dex.swap_output(
        reserve_in, reserve_out, coins_to_units(amount_in), _document_int(pool, "fee_bps")
    )
    minimum = (
        coins_to_units(min_out)
        if min_out is not None
        else quote * (10_000 - slippage_bps) // 10_000
    )
    wallet = _wallet_for(ctx, key, network)
    with _account_source(ctx, params, wallet.address, node) as accounts:
        tx = _run(
            lambda: accounts.service.build_swap(
                wallet.signer,
                pool_id=pool_id,
                asset_in=asset_in,
                amount_in=coins_to_units(amount_in),
                min_amount_out=minimum,
                fee=fee,
            )
        )
    receipt = _submit_transaction(node or DEFAULT_NODE_URL, tx)
    _report_transaction(
        ctx,
        tx,
        receipt,
        f"swapped {_coins(coins_to_units(amount_in))} for at least {_coins(minimum)}",
    )


@bridge_app.command("info")
def bridge_info(
    ctx: typer.Context,
    node: Annotated[str | None, typer.Option("--node", help="Node that serves /v1/bridge.")] = None,
) -> None:
    """Show the Ethereum environment this node is paired with."""
    document = _node_document(node, "/v1/bridge")
    if _options(ctx).json_output:
        _echo_json(document)
        return
    typer.echo(
        f"{document.get('network')} -> {document.get('eth_chain_name')} "
        f"(chain {document.get('eth_chain_id')})"
    )
    typer.echo(f"contract: {document.get('eth_htlc_contract') or 'not configured'}")
    typer.echo(f"rpc: {document.get('eth_rpc_url') or 'not configured'}")
    typer.echo(
        f"confirmations: {document.get('eth_confirmations')} "
        f"sccp margin: {document.get('sccp_margin')}"
    )


@bridge_app.command("orders")
def bridge_orders(
    ctx: typer.Context,
    maker: Annotated[
        str | None, typer.Option("--maker", help="Filter by maker SCCP address.")
    ] = None,
    node: Annotated[
        str | None, typer.Option("--node", help="Node that serves the order book.")
    ] = None,
) -> None:
    """List open sell-SCCP orders, best price first."""
    path = "/v1/orders" if maker is None else f"/v1/orders?maker_sccp={maker}"
    document = _node_document(node, path)
    if _options(ctx).json_output:
        _echo_json(document)
        return
    rows = document.get("orders")
    if not isinstance(rows, list) or not rows:
        typer.echo("no open orders")
        return
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        typer.echo(
            f"{row.get('order_id')} {_coins(int(row.get('give_amount', 0)))} SCCP "
            f"for {row.get('want_amount')} wei maker {row.get('maker_sccp')}"
        )


@bridge_app.command("order")
def bridge_order(
    ctx: typer.Context,
    order_id: Annotated[str, typer.Option("--order-id", help="Order id to inspect.")],
    node: Annotated[
        str | None, typer.Option("--node", help="Node that serves the order book.")
    ] = None,
) -> None:
    """Show one order and its take, if a taker has locked ETH."""
    document = _node_document(node, f"/v1/orders/{order_id}")
    if _options(ctx).json_output:
        _echo_json(document)
        return
    order = document.get("order")
    if not isinstance(order, Mapping):
        _fail("the node returned no order")
    typer.echo(
        f"{order_id} {_coins(int(order.get('give_amount', 0)))} SCCP for "
        f"{order.get('want_amount')} wei"
    )
    take = document.get("take")
    if isinstance(take, Mapping):
        typer.echo(f"take: {take.get('take_id')}")
        typer.echo(f"  taker SCCP {take.get('taker_sccp')}")
        typer.echo(f"  taker ETH  {take.get('taker_eth')}")
        typer.echo(f"  hashlock   {take.get('hashlock')}")
        typer.echo(f"  ETH tx     {take.get('eth_tx_hash')}")
        typer.echo(f"  ETH lock   {take.get('eth_timelock')}")
        typer.echo(f"  SCCP lock  {take.get('sccp_timelock_height')}")
    else:
        typer.echo("take: none")


@bridge_app.command("htlcs")
def bridge_htlcs(
    ctx: typer.Context,
    address: Annotated[str | None, typer.Option("--address", help="Filter by participant.")] = None,
    hashlock: Annotated[str | None, typer.Option("--hashlock", help="Filter by hashlock.")] = None,
    node: Annotated[str | None, typer.Option("--node", help="Node that serves the locks.")] = None,
) -> None:
    """List SCCP HTLCs involving an address or one hashlock."""
    query = []
    if address is not None:
        query.append(f"address={address}")
    if hashlock is not None:
        query.append(f"hashlock={hashlock}")
    path = "/v1/htlcs" + ("?" + "&".join(query) if query else "")
    document = _node_document(node, path)
    if _options(ctx).json_output:
        _echo_json(document)
        return
    rows = document.get("htlcs")
    if not isinstance(rows, list) or not rows:
        typer.echo("no locks")
        return
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        typer.echo(
            f"{row.get('htlc_id')} {row.get('status')} {row.get('amount')} "
            f"lock {row.get('timelock_height')} "
            f"{row.get('sender')} -> {row.get('beneficiary')}"
        )


@bridge_app.command("secret")
def bridge_secret(ctx: typer.Context) -> None:
    """Generate a 32-byte preimage and its SHA-256 hashlock."""
    preimage = secrets.token_hex(32)
    digest = sha256(bytes.fromhex(preimage)).hexdigest()
    if _options(ctx).json_output:
        _echo_json({"preimage": preimage, "hashlock": digest})
        return
    typer.echo(f"preimage: {preimage}")
    typer.echo(f"hashlock: {digest}")


@bridge_app.command("sell")
def bridge_sell(
    ctx: typer.Context,
    key: Annotated[Path, typer.Option("--key", help="Maker's SCCP keyfile.")],
    eth_address: Annotated[str, typer.Option("--eth-address", help="Maker's Ethereum address.")],
    amount: Annotated[int, typer.Option("--amount", help="SCCP to sell, in whole coins.")],
    price_wei: Annotated[
        str,
        typer.Option("--price-wei", help="ETH price as a positive wei decimal string."),
    ],
    expiry_hours: Annotated[
        float,
        typer.Option("--expiry-hours", help="How long the order stays open."),
    ] = 24.0,
    min_sccp_timelock: Annotated[
        int | None,
        typer.Option("--min-sccp-timelock", help="Minimum SCCP timelock height."),
    ] = None,
    nonce: Annotated[
        int | None,
        typer.Option("--order-nonce", help="Order nonce (default: current milliseconds)."),
    ] = None,
    node: Annotated[str | None, typer.Option("--node", help="Node that accepts the order.")] = None,
    network: Annotated[str | None, typer.Option("--network", help="Require this network.")] = None,
) -> None:
    """Publish a signed order to sell SCCP for ETH."""
    if amount <= 0:
        _fail("--amount must be positive", code=2)
    if expiry_hours <= 0:
        _fail("--expiry-hours must be positive", code=2)
    params = _resolve_network(ctx, network)
    wallet = _wallet_for(ctx, key, network)
    bridge = _node_document(node, "/v1/bridge")
    if bridge.get("configured") is not True:
        _fail(
            "the node has no Ethereum HTLC contract configured; set "
            "SCCP_ETH_HTLC_CONTRACT before publishing an order"
        )
    give_amount = coins_to_units(amount)
    account = _node_document(node, f"/v1/accounts/{wallet.address}")
    if _document_int(account, "balance") < give_amount + params.min_fee:
        _fail(
            f"{wallet.address} holds {_coins(_document_int(account, 'balance'))} SCCP; "
            f"the order needs {_coins(give_amount + params.min_fee)}"
        )
    status = _node_document(node, "/v1/status")
    minimum = min_sccp_timelock or _document_int(status, "height") + 1
    order = _run(
        lambda: sign_order(
            wallet.signer,
            params,
            maker_eth=eth_address,
            give_amount=give_amount,
            want_amount=price_wei,
            min_sccp_timelock=minimum,
            expiry=int(time.time()) + int(expiry_hours * 3600),
            nonce=nonce if nonce is not None else int(time.time() * 1000),
        )
    )
    accepted = _node_document(node, "/v1/orders", order.model_dump(mode="json"))
    if _options(ctx).json_output:
        _echo_json({**accepted, "order": order.model_dump(mode="json")})
        return
    typer.echo(f"published order {_document_text(accepted, 'order_id')}")
    typer.echo(f"  {_coins(give_amount)} SCCP for {price_wei} wei")


@bridge_app.command("take")
def bridge_take(
    ctx: typer.Context,
    key: Annotated[Path, typer.Option("--key", help="Taker's SCCP keyfile.")],
    order_id: Annotated[str, typer.Option("--order-id", help="Order to take.")],
    eth_address: Annotated[str, typer.Option("--eth-address", help="Taker's Ethereum address.")],
    hashlock: Annotated[str, typer.Option("--hashlock", help="SHA-256 of the shared preimage.")],
    eth_timelock: Annotated[
        int, typer.Option("--eth-timelock", help="ETH unlock time, Unix seconds.")
    ],
    eth_tx_hash: Annotated[str, typer.Option("--eth-tx-hash", help="The lock transaction hash.")],
    sccp_timelock: Annotated[
        int | None,
        typer.Option("--sccp-timelock-height", help="SCCP expiry height (default: safe maximum)."),
    ] = None,
    nonce: Annotated[
        int | None,
        typer.Option("--take-nonce", help="Take nonce (default: current milliseconds)."),
    ] = None,
    node: Annotated[str | None, typer.Option("--node", help="Node that accepts the take.")] = None,
    network: Annotated[str | None, typer.Option("--network", help="Require this network.")] = None,
) -> None:
    """Publish the take after locking the ETH leg."""
    params = _resolve_network(ctx, network)
    wallet = _wallet_for(ctx, key, network)
    detail = _node_document(node, f"/v1/orders/{order_id}")
    order = detail.get("order")
    if not isinstance(order, Mapping):
        _fail(f"order {order_id} is not open")
    environment = _bridge_environment_for(params)
    if sccp_timelock is None:
        status = _node_document(node, "/v1/status")
        minimum = _document_int(order, "min_sccp_timelock")
        try:
            sccp_timelock = sccp_timelock_height(
                _document_int(status, "height"),
                eth_timelock,
                int(time.time()),
                params.target_block_seconds,
                environment.sccp_margin,
                minimum,
            )
        except OrderError as exc:
            _fail(str(exc))
    take = _run(
        lambda: sign_take(
            wallet.signer,
            params,
            order=order_id,
            taker_eth=eth_address,
            hashlock=hashlock,
            eth_timelock=eth_timelock,
            sccp_timelock_height=sccp_timelock,
            eth_tx_hash=eth_tx_hash,
            nonce=nonce if nonce is not None else int(time.time() * 1000),
        )
    )
    accepted = _node_document(node, f"/v1/orders/{order_id}/takes", take.model_dump(mode="json"))
    if _options(ctx).json_output:
        _echo_json({**accepted, "take": take.model_dump(mode="json")})
        return
    typer.echo(f"published take {_document_text(accepted, 'take_id')}")
    typer.echo(f"  SCCP timelock height {sccp_timelock}; ETH timelock {eth_timelock}")


@bridge_app.command("htlc")
def bridge_htlc(
    ctx: typer.Context,
    htlc_id_value: Annotated[str, typer.Option("--htlc-id", help="Lock id to inspect.")],
    node: Annotated[str | None, typer.Option("--node", help="Node that serves the lock.")] = None,
) -> None:
    """Show one SCCP HTLC, including the preimage once claimed."""
    document = _node_document(node, f"/v1/htlcs/{htlc_id_value}")
    if _options(ctx).json_output:
        _echo_json(document)
        return
    for key in (
        "htlc_id",
        "status",
        "sender",
        "beneficiary",
        "hashlock",
        "timelock_height",
        "amount",
        "created_at_height",
        "closed_at_height",
        "preimage",
    ):
        typer.echo(f"{key}: {document.get(key)}")


@bridge_app.command("lock")
def bridge_lock(
    ctx: typer.Context,
    key: Annotated[Path, typer.Option("--key", help="SCCP keyfile that locks the funds.")],
    hashlock: Annotated[str, typer.Option("--hashlock", help="SHA-256 of the shared preimage.")],
    timelock_height: Annotated[
        int,
        typer.Option("--timelock-height", help="Absolute SCCP height after which refund is valid."),
    ],
    beneficiary: Annotated[str, typer.Option("--beneficiary", help="SCCP address that may claim.")],
    amount: Annotated[int, typer.Option("--amount", help="Amount to lock, in whole coins.")],
    asset: Annotated[
        str | None,
        typer.Option("--asset", help="Asset id/name/symbol (omit for SCCP)."),
    ] = None,
    fee: Annotated[int | None, typer.Option("--fee", help="Fee in base units.")] = None,
    node: Annotated[
        str | None, typer.Option("--node", help="Node that receives the transaction.")
    ] = None,
    network: Annotated[str | None, typer.Option("--network", help="Require this network.")] = None,
) -> None:
    """Lock SCCP under a hashlock for a bridge counterparty."""
    if amount <= 0:
        _fail("--amount must be positive", code=2)
    params = _resolve_network(ctx, network)
    wallet = _wallet_for(ctx, key, network)
    with _account_source(ctx, params, wallet.address, node) as accounts:
        tx = _run(
            lambda: accounts.service.build_htlc_lock(
                wallet.signer,
                hashlock=hashlock,
                timelock_height=timelock_height,
                beneficiary=beneficiary,
                amount=coins_to_units(amount),
                fee=fee,
                asset=asset,
            )
        )
    receipt = _submit_transaction(node or DEFAULT_NODE_URL, tx)
    payload = {**_transaction_summary(tx, receipt), "htlc_id": htlc_id(tx.sender, tx.nonce)}
    if _options(ctx).json_output:
        _echo_json(payload)
        return
    typer.echo(f"locked {_coins(coins_to_units(amount))}")
    typer.echo(f"htlc: {payload['htlc_id']}")
    typer.echo(f"txid: {payload['txid']}")


@bridge_app.command("claim")
def bridge_claim(
    ctx: typer.Context,
    key: Annotated[Path, typer.Option("--key", help="Beneficiary's SCCP keyfile.")],
    htlc_id_value: Annotated[str, typer.Option("--htlc-id", help="Lock id to claim.")],
    preimage: Annotated[
        str, typer.Option("--preimage", help="32-byte secret that opens the lock.")
    ],
    fee: Annotated[int | None, typer.Option("--fee", help="Fee in base units.")] = None,
    node: Annotated[
        str | None, typer.Option("--node", help="Node that receives the transaction.")
    ] = None,
    network: Annotated[str | None, typer.Option("--network", help="Require this network.")] = None,
) -> None:
    """Claim SCCP and reveal the preimage."""
    params = _resolve_network(ctx, network)
    wallet = _wallet_for(ctx, key, network)
    document = _node_document(node, f"/v1/htlcs/{htlc_id_value}")
    record = _run(lambda: HtlcRecord.from_dict(htlc_id_value, document))
    with _account_source(ctx, params, wallet.address, node) as accounts:
        tx = _run(
            lambda: accounts.service.build_htlc_claim(
                wallet.signer,
                htlc_id=htlc_id_value,
                preimage=preimage,
                fee=fee,
                record=record,
            )
        )
    receipt = _submit_transaction(node or DEFAULT_NODE_URL, tx)
    _report_transaction(ctx, tx, receipt, "claimed SCCP and revealed the preimage")


@bridge_app.command("refund")
def bridge_refund(
    ctx: typer.Context,
    key: Annotated[Path, typer.Option("--key", help="SCCP keyfile that created the lock.")],
    htlc_id_value: Annotated[str, typer.Option("--htlc-id", help="Lock id to refund.")],
    fee: Annotated[int | None, typer.Option("--fee", help="Fee in base units.")] = None,
    node: Annotated[
        str | None, typer.Option("--node", help="Node that receives the transaction.")
    ] = None,
    network: Annotated[str | None, typer.Option("--network", help="Require this network.")] = None,
) -> None:
    """Refund an expired SCCP lock to its creator."""
    params = _resolve_network(ctx, network)
    wallet = _wallet_for(ctx, key, network)
    document = _node_document(node, f"/v1/htlcs/{htlc_id_value}")
    record = _run(lambda: HtlcRecord.from_dict(htlc_id_value, document))
    with _account_source(ctx, params, wallet.address, node) as accounts:
        tx = _run(
            lambda: accounts.service.build_htlc_refund(
                wallet.signer,
                htlc_id=htlc_id_value,
                fee=fee,
                record=record,
            )
        )
    receipt = _submit_transaction(node or DEFAULT_NODE_URL, tx)
    _report_transaction(ctx, tx, receipt, "refunded SCCP")


@bridge_app.command("eth-lock")
def bridge_eth_lock(
    ctx: typer.Context,
    sender: Annotated[
        str, typer.Option("--from", help="Unlocked Ethereum account that locks ETH.")
    ],
    hashlock: Annotated[str, typer.Option("--hashlock", help="SHA-256 of the shared preimage.")],
    timelock: Annotated[int, typer.Option("--timelock", help="ETH unlock time, Unix seconds.")],
    beneficiary: Annotated[
        str, typer.Option("--beneficiary", help="Maker's Ethereum address that may claim.")
    ],
    value_wei: Annotated[int, typer.Option("--value-wei", help="ETH to lock, in wei.")],
    rpc_url: Annotated[
        str | None, typer.Option("--rpc-url", help="Override the bridge RPC.")
    ] = None,
    contract: Annotated[
        str | None, typer.Option("--contract", help="Override the HTLC address.")
    ] = None,
    confirmations: Annotated[
        int | None, typer.Option("--confirmations", help="Override the confirmation depth.")
    ] = None,
    node: Annotated[
        str | None, typer.Option("--node", help="Node that publishes /v1/bridge.")
    ] = None,
    network: Annotated[str | None, typer.Option("--network", help="Require this network.")] = None,
) -> None:
    """Lock ETH through the node's configured EthHtlc contract.

    The JSON-RPC node must have ``--from`` unlocked. That is the default on
    Anvil; on a production network use the Bridge page and an EIP-1193 wallet.
    """
    if value_wei <= 0:
        _fail("--value-wei must be positive", code=2)
    _, rpc, address, depth = _eth_endpoint(
        ctx,
        node=node,
        network=network,
        rpc_url=rpc_url,
        contract=contract,
        confirmations=confirmations,
    )
    tx_hash, lock_id = _run(
        lambda: lock_eth(
            rpc,
            address,
            sender=sender,
            hashlock=hashlock,
            timelock=timelock,
            beneficiary=beneficiary,
            value=value_wei,
            confirmations=depth,
        )
    )
    payload = {"tx_hash": tx_hash, "lock_id": lock_id, "value_wei": value_wei}
    if _options(ctx).json_output:
        _echo_json(payload)
        return
    typer.echo(f"locked {value_wei} wei")
    typer.echo(f"lock: {lock_id}")
    typer.echo(f"tx:   {tx_hash}")


@bridge_app.command("eth-claim")
def bridge_eth_claim(
    ctx: typer.Context,
    sender: Annotated[str, typer.Option("--from", help="Unlocked maker account that claims.")],
    lock_id: Annotated[str, typer.Option("--lock-id", help="Ethereum lock id.")],
    preimage: Annotated[str, typer.Option("--preimage", help="Revealed 32-byte secret.")],
    rpc_url: Annotated[
        str | None, typer.Option("--rpc-url", help="Override the bridge RPC.")
    ] = None,
    contract: Annotated[
        str | None, typer.Option("--contract", help="Override the HTLC address.")
    ] = None,
    confirmations: Annotated[
        int | None, typer.Option("--confirmations", help="Override the confirmation depth.")
    ] = None,
    node: Annotated[
        str | None, typer.Option("--node", help="Node that publishes /v1/bridge.")
    ] = None,
    network: Annotated[str | None, typer.Option("--network", help="Require this network.")] = None,
) -> None:
    """Claim the ETH leg with the preimage the SCCP claim revealed."""
    _, rpc, address, depth = _eth_endpoint(
        ctx,
        node=node,
        network=network,
        rpc_url=rpc_url,
        contract=contract,
        confirmations=confirmations,
    )
    tx_hash = _run(
        lambda: claim_eth(
            rpc,
            address,
            sender=sender,
            lock_id=lock_id,
            preimage=preimage,
            confirmations=depth,
        )
    )
    if _options(ctx).json_output:
        _echo_json({"tx_hash": tx_hash, "lock_id": lock_id})
        return
    typer.echo(f"claimed ETH lock {lock_id}")
    typer.echo(f"tx: {tx_hash}")


@bridge_app.command("eth-refund")
def bridge_eth_refund(
    ctx: typer.Context,
    sender: Annotated[str, typer.Option("--from", help="Unlocked depositor account.")],
    lock_id: Annotated[str, typer.Option("--lock-id", help="Ethereum lock id.")],
    rpc_url: Annotated[
        str | None, typer.Option("--rpc-url", help="Override the bridge RPC.")
    ] = None,
    contract: Annotated[
        str | None, typer.Option("--contract", help="Override the HTLC address.")
    ] = None,
    confirmations: Annotated[
        int | None, typer.Option("--confirmations", help="Override the confirmation depth.")
    ] = None,
    node: Annotated[
        str | None, typer.Option("--node", help="Node that publishes /v1/bridge.")
    ] = None,
    network: Annotated[str | None, typer.Option("--network", help="Require this network.")] = None,
) -> None:
    """Refund an expired ETH lock to the account that deposited it."""
    _, rpc, address, depth = _eth_endpoint(
        ctx,
        node=node,
        network=network,
        rpc_url=rpc_url,
        contract=contract,
        confirmations=confirmations,
    )
    tx_hash = _run(
        lambda: refund_eth(
            rpc,
            address,
            sender=sender,
            lock_id=lock_id,
            confirmations=depth,
        )
    )
    if _options(ctx).json_output:
        _echo_json({"tx_hash": tx_hash, "lock_id": lock_id})
        return
    typer.echo(f"refunded ETH lock {lock_id}")
    typer.echo(f"tx: {tx_hash}")


@bridge_app.command("eth-lock-info")
def bridge_eth_lock_info(
    ctx: typer.Context,
    lock_id: Annotated[str, typer.Option("--lock-id", help="Ethereum lock id.")],
    rpc_url: Annotated[
        str | None, typer.Option("--rpc-url", help="Override the bridge RPC.")
    ] = None,
    contract: Annotated[
        str | None, typer.Option("--contract", help="Override the HTLC address.")
    ] = None,
    node: Annotated[
        str | None, typer.Option("--node", help="Node that publishes /v1/bridge.")
    ] = None,
    network: Annotated[str | None, typer.Option("--network", help="Require this network.")] = None,
) -> None:
    """Read one EthHtlc lock."""
    _, rpc, address, _ = _eth_endpoint(
        ctx,
        node=node,
        network=network,
        rpc_url=rpc_url,
        contract=contract,
        confirmations=None,
    )
    stored = _run(lambda: get_lock(rpc, address, lock_id))
    if _options(ctx).json_output:
        _echo_json(
            {
                "lock_id": lock_id,
                "depositor": stored.depositor,
                "beneficiary": stored.beneficiary,
                "hashlock": stored.hashlock,
                "timelock": stored.timelock,
                "amount": stored.amount,
                "claimed": stored.claimed,
                "refunded": stored.refunded,
            }
        )
        return
    typer.echo(f"lock {lock_id}")
    typer.echo(f"depositor:   {stored.depositor}")
    typer.echo(f"beneficiary: {stored.beneficiary}")
    typer.echo(f"hashlock:    {stored.hashlock}")
    typer.echo(f"timelock:    {stored.timelock}")
    typer.echo(f"amount:      {stored.amount} wei")
    typer.echo(f"closed:      claimed={stored.claimed} refunded={stored.refunded}")


if __name__ == "__main__":  # pragma: no cover - console-script parity
    app()
