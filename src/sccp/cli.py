"""``sccp`` command line interface (docs/IMPLEMENTATION_PLAN.md section 5.13).

M0 shipped ``--version`` and the wallet basics; M1 added the offline PGN
verifier (``verify pgn``); M2 adds offline proof verification (``verify proof``)
and the matchmaking relay (``matchmaker run``). Every command is a thin wrapper
over a library call so the same behaviour is testable in-process.

Global options may be passed before or after the subcommand; an explicitly
passed ``--network`` always wins over the global default of ``testnet``.
"""

from __future__ import annotations

import json as jsonlib
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, NoReturn, TypeVar

import typer

from sccp import version as version_module
from sccp.chess.openings import load_book
from sccp.chess.pgn import GameResult, validate_pgn_file
from sccp.config import NetworkParams, get_network
from sccp.crypto.wallet import (
    DEFAULT_KDF,
    KdfParams,
    Wallet,
    create_wallet,
    export_mnemonic,
    import_mnemonic,
    read_header,
    read_passphrase_file,
)
from sccp.errors import SccpError
from sccp.miner.matchmaker import Matchmaker
from sccp.miner.matchmaker import create_app as create_matchmaker_app
from sccp.protocol.models import WinProof
from sccp.protocol.verification import (
    ProofContext,
    context_from_document,
    verify_win_proof,
)

DEFAULT_NETWORK = "testnet"
PASSPHRASE_ENV = "SCCP_WALLET_PASSPHRASE"

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
                "keyfile": str(wallet.path) if wallet.path is not None else None,
            }
        )
        return
    typer.echo(wallet.address)


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
        int,
        typer.Option("--kdf-n", hidden=True, help="scrypt N (tests only; default 2**15)."),
    ] = DEFAULT_KDF.n,
) -> None:
    """Create an encrypted keyfile and print its address."""
    params = _resolve_network(ctx, network)
    passphrase = _passphrase(ctx, confirm=True)
    wallet = _run(
        lambda: create_wallet(
            out,
            params,
            passphrase,
            kdf=KdfParams(n=kdf_n),
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
        _echo_json({"mnemonic": phrase, "address": header.address, "network": header.network})
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
    kdf_n: Annotated[
        int,
        typer.Option("--kdf-n", hidden=True, help="scrypt N (tests only; default 2**15)."),
    ] = DEFAULT_KDF.n,
) -> None:
    """Recreate a keyfile from a 24-word backup phrase."""
    params = _resolve_network(ctx, network)
    phrase = typer.prompt("Mnemonic (24 words)", hide_input=True)
    passphrase = _passphrase(ctx, confirm=True)
    wallet = _run(
        lambda: import_mnemonic(
            out,
            phrase,
            params,
            passphrase,
            kdf=KdfParams(n=kdf_n),
            created_at=created_at,
            force=force,
        )
    )
    _emit_wallet(ctx, wallet)


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
    store: Annotated[
        Path | None,
        typer.Option("--store", help="JSON store that survives a relay restart."),
    ] = None,
    round_number: Annotated[
        int | None,
        typer.Option("--round", help="Open this round at startup."),
    ] = None,
) -> None:
    """Serve the matchmaking relay over HTTP (uvicorn)."""
    params = _resolve_network(ctx, None)
    document = _read_json(config)
    if not isinstance(document, Mapping):
        _fail(f"{config} must contain a JSON object")
    if round_number is not None:
        document = {**document, "open_round": round_number}
    state = _run(lambda: Matchmaker.from_document(document, params=params, store=store))
    typer.echo(
        f"matchmaker on {host}:{port} for {params.network_id} epoch {state.beacon.epoch} "
        f"({len(state.matches())} matches)"
    )
    import uvicorn

    uvicorn.run(create_matchmaker_app(state), host=host, port=port)
