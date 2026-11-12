#!/usr/bin/env python3
"""Export the chess games recorded in a block as PGN files.

A block carries the ``WinProof`` objects that paid for it, and every proof
carries the full co-signed PGN. This script asks a running node for its tip
(``GET /v1/status``), fetches that block (``GET /v1/blocks/{height}``) -- or the
block named by ``--block`` -- and writes one ``.pgn`` per game plus a
``manifest.json`` describing the block and the games. The manifest records the
proof and match identifiers, so an exported file can be traced back to the
chain without opening it.

Nothing is written until every game has been replayed. ``validate_pgn`` checks
each PGN against the certificate in its own proof (match id, epoch, round,
seats, engines, opening, result and termination), and the derived game hash,
winner and ply count are compared with the proof. A node that serves a
truncated or inconsistent proof therefore fails the export instead of writing a
file that merely looks like a game.

Examples::

    python scripts/export_block_games.py
    python scripts/export_block_games.py --node http://127.0.0.1:8645 --out games
    python scripts/export_block_games.py --block 42 --out games
    python scripts/export_block_games.py --block <64-hex block hash> --out games

The default node URL is the one ``sccp node run`` listens on. A block with no
proofs (genesis, or an early bootstrap block) is a successful, empty export.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import httpx
from pydantic import ValidationError

from sccp.chess.pgn import ParsedGame, validate_pgn
from sccp.config import NetworkParams
from sccp.errors import SccpError
from sccp.node.models import BlockView, NodeStatus
from sccp.protocol.models import WinProof
from sccp.protocol.transcript import expected_from_certificate

DEFAULT_NODE: Final = "http://127.0.0.1:8645"
"""Where ``sccp node run`` listens by default."""

DEFAULT_OUT: Final = Path("block-games")
"""Directory the exported games are written to when ``--out`` is omitted."""

MANIFEST_NAME: Final = "manifest.json"
"""The index written alongside the PGN files."""

DEFAULT_TIMEOUT: Final = 10.0
"""Seconds to wait for each node request."""

EXAMPLES: Final = """\
examples:
  python scripts/export_block_games.py
  python scripts/export_block_games.py --node http://127.0.0.1:8645 --out games
  python scripts/export_block_games.py --block 42 --out games
"""


class ExportError(SccpError):
    """A node response, a proof or an output directory cannot be exported."""


@dataclass(frozen=True, slots=True)
class ExportedGame:
    """One PGN file written for one proof in the block."""

    path: Path
    proof: WinProof


@dataclass(frozen=True, slots=True)
class ExportResult:
    """Everything the export produced, for reporting and for tests."""

    status: NodeStatus
    block: BlockView
    directory: Path
    games: tuple[ExportedGame, ...]
    manifest: Path


def _get_json(client: httpx.Client, path: str) -> dict[str, Any]:
    """GET ``path`` and return a JSON object, translating every failure."""
    try:
        response = client.get(path)
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text.strip()
        suffix = f": {detail}" if detail else ""
        raise ExportError(f"{path} returned HTTP {exc.response.status_code}{suffix}") from None
    except httpx.HTTPError as exc:
        raise ExportError(f"cannot fetch {path} from {client.base_url}: {exc}") from None
    try:
        document = response.json()
    except ValueError as exc:
        raise ExportError(f"{path} did not return JSON: {exc}") from None
    if not isinstance(document, dict):
        raise ExportError(f"{path} did not return a JSON object")
    return document


def fetch_status(client: httpx.Client) -> NodeStatus:
    """The node's status document, validated against the API contract."""
    document = _get_json(client, "/v1/status")
    try:
        return NodeStatus.model_validate(document)
    except ValidationError as exc:
        raise ExportError(f"/v1/status does not match the node API: {exc}") from None


def fetch_params(client: httpx.Client) -> NetworkParams:
    """The node's consensus parameters, used to validate exported PGNs."""
    document = _get_json(client, "/v1/params")
    try:
        return NetworkParams.model_validate(document)
    except ValidationError as exc:
        raise ExportError(f"/v1/params does not match the node API: {exc}") from None


def fetch_block(client: httpx.Client, reference: str) -> BlockView:
    """One block plus its position, by height or hash."""
    document = _get_json(client, f"/v1/blocks/{reference}")
    try:
        return BlockView.model_validate(document)
    except ValidationError as exc:
        raise ExportError(f"/v1/blocks/{reference} does not match the node API: {exc}") from None


def _check_proof(proof: WinProof, params: NetworkParams) -> ParsedGame:
    """Replay a proof's PGN and confirm it describes the game the proof claims."""
    if proof.network != params.network_id:
        raise ExportError(
            f"game {proof.game_hash}: proof is for {proof.network!r}, "
            f"the node is on {params.network_id!r}"
        )
    certificate = proof.certificate
    if certificate.network != proof.network:
        raise ExportError(f"game {proof.game_hash}: certificate is for another network")
    if certificate.match_id != proof.match_id:
        raise ExportError(f"game {proof.game_hash}: certificate is for another match")
    if (certificate.epoch, certificate.round) != (proof.epoch, proof.round):
        raise ExportError(f"game {proof.game_hash}: certificate is for another round")
    if certificate.opening != proof.opening:
        raise ExportError(f"game {proof.game_hash}: certificate and proof openings disagree")
    winner = certificate.white if proof.winner_seat == "white" else certificate.black
    loser = certificate.black if proof.winner_seat == "white" else certificate.white
    if (proof.winner, proof.loser) != (winner, loser):
        raise ExportError(f"game {proof.game_hash}: winner and seat disagree")
    try:
        parsed = validate_pgn(
            proof.pgn,
            params=params,
            expected=expected_from_certificate(certificate),
            require_win=True,
            clock_evidence=proof.clock_evidence,
        )
    except SccpError as exc:
        raise ExportError(f"game {proof.game_hash}: {exc}") from exc
    if parsed.game_hash != proof.game_hash:
        raise ExportError(f"game {proof.game_hash}: PGN hashes to {parsed.game_hash}")
    if (parsed.result.value, parsed.termination.value) != (
        proof.result,
        proof.termination,
    ):
        raise ExportError(f"game {proof.game_hash}: PGN result and proof disagree")
    if parsed.plies != proof.plies:
        raise ExportError(
            f"game {proof.game_hash}: PGN has {parsed.plies} plies, proof says {proof.plies}"
        )
    if parsed.low_ply != proof.low_ply:
        raise ExportError(f"game {proof.game_hash}: PGN and proof disagree on low_ply")
    winner_seat = "white" if parsed.result.value == "1-0" else "black"
    if winner_seat != proof.winner_seat:
        raise ExportError(f"game {proof.game_hash}: PGN result and winner seat disagree")
    return parsed


def _game_entry(proof: WinProof, file_name: str) -> dict[str, Any]:
    """The manifest row for one exported game."""
    certificate = proof.certificate
    return {
        "file": file_name,
        "game_hash": proof.game_hash,
        "proof_id": proof.proof_id,
        "match_id": proof.match_id,
        "epoch": proof.epoch,
        "round": proof.round,
        "white": certificate.white,
        "black": certificate.black,
        "engine_white": certificate.engine_white,
        "engine_black": certificate.engine_black,
        "winner": proof.winner,
        "loser": proof.loser,
        "winner_seat": proof.winner_seat,
        "result": proof.result,
        "termination": proof.termination,
        "plies": proof.plies,
        "low_ply": proof.low_ply,
        "opening": proof.opening.model_dump(mode="json"),
        "time_control": certificate.time_control.model_dump(mode="json"),
    }


def _manifest(status: NodeStatus, block: BlockView, games: list[dict[str, Any]]) -> dict[str, Any]:
    """The block-level index written next to the PGN files."""
    header = block.block.header
    return {
        "network": status.network,
        "block": {
            "height": block.height,
            "hash": block.hash,
            "parent_hash": block.parent_hash,
            "main_chain": block.main_chain,
            "epoch": header.epoch,
            "timestamp": header.timestamp,
            "miner": header.miner,
            "wins_required": header.wins_required,
        },
        "games": games,
    }


def export_block_games(
    client: httpx.Client,
    *,
    output_dir: Path,
    block_reference: str | None = None,
    report: Callable[[str], None] | None = None,
) -> ExportResult:
    """Fetch one block and write its games into ``output_dir``.

    ``block_reference`` is a height or hash; ``None`` asks the node for its
    current tip. ``report`` receives a progress line for each game as it is
    checked; the default writes nothing, so tests stay quiet.
    """
    status = fetch_status(client)
    reference = block_reference or str(status.height)
    block = fetch_block(client, reference)
    if block.network != status.network:
        raise ExportError(
            f"block {block.hash} is for {block.network!r}, the node reports {status.network!r}"
        )
    params = fetch_params(client)
    if params.network_id != status.network:
        raise ExportError(
            f"node parameters are for {params.network_id!r}, status says {status.network!r}"
        )

    checked: list[tuple[WinProof, str, dict[str, Any]]] = []
    seen: set[str] = set()
    for proof in block.block.proofs:
        if proof.game_hash in seen:
            raise ExportError(f"game {proof.game_hash} appears twice in the block")
        seen.add(proof.game_hash)
        _check_proof(proof, params)
        file_name = f"game-{proof.game_hash}.pgn"
        checked.append((proof, file_name, _game_entry(proof, file_name)))
        if report is not None:
            report(f"checked {file_name}")

    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ExportError(f"cannot create {output_dir}: {exc}") from None

    games: list[ExportedGame] = []
    for proof, file_name, _ in checked:
        path = output_dir / file_name
        try:
            path.write_text(proof.pgn, encoding="utf-8")
        except OSError as exc:
            raise ExportError(f"cannot write {path}: {exc}") from None
        games.append(ExportedGame(path=path, proof=proof))

    manifest_path = output_dir / MANIFEST_NAME
    document = _manifest(status, block, [entry for _, _, entry in checked])
    try:
        manifest_path.write_text(
            json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    except OSError as exc:
        raise ExportError(f"cannot write {manifest_path}: {exc}") from None

    return ExportResult(
        status=status,
        block=block,
        directory=output_dir,
        games=tuple(games),
        manifest=manifest_path,
    )


def _report(result: ExportResult, out: Callable[[str], None] = print) -> None:
    """Print a short summary of what was written."""
    block = result.block
    plural = "s" if len(result.games) != 1 else ""
    out(f"block {block.height} {block.hash} ({len(result.games)} game{plural})")
    for game in result.games:
        proof = game.proof
        certificate = proof.certificate
        out(
            f"  {game.path}  {certificate.white} vs {certificate.black}  "
            f"{proof.result} by {proof.termination} ({proof.plies} plies)"
        )
    out(f"manifest {result.manifest}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="export_block_games.py",
        description="Export the chess games recorded in a block as PGN files.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=EXAMPLES,
    )
    parser.add_argument(
        "--node",
        default=DEFAULT_NODE,
        metavar="URL",
        help=f"node base URL (default: {DEFAULT_NODE})",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT,
        metavar="DIR",
        help=f"directory for the PGN files and manifest (default: {DEFAULT_OUT})",
    )
    parser.add_argument(
        "--block",
        metavar="REFERENCE",
        help="block height or hash to export (default: the node's current tip)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        metavar="SECONDS",
        help=f"timeout for each node request (default: {DEFAULT_TIMEOUT:g})",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Export the tip (or ``--block``) and report the files written."""
    args = _parser().parse_args(argv)
    try:
        with httpx.Client(base_url=args.node, timeout=args.timeout) as client:
            result = export_block_games(
                client,
                output_dir=args.out,
                block_reference=args.block or None,
            )
    except SccpError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    _report(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
