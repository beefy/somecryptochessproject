"""The move loop: two engines to a replayable PGN (PROTOCOL.md section 8).

The runner is the single place that decides a move is legal. Every move from an
engine goes through ``Board.is_legal`` before it is pushed, so a misbehaving
adapter can only waste our time -- it cannot corrupt the transcript.

No wall-clock value ever reaches the result: ``RawGame`` carries SAN, the
result, the termination and the final board, and the caller adds certificate
headers. That keeps ``play_game`` deterministic for a seeded mock matchup.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

import chess
import chess.pgn

from sccp.chess.engines import Engine
from sccp.chess.openings import OpeningLine
from sccp.chess.pgn import GameResult, Termination, outcome_result
from sccp.chess.rulesets import CHESS960_VARIANT, ruleset_for
from sccp.chess.variants import board_for_variant, normalise_start_fen
from sccp.config import STANDARD_VARIANT, NetworkParams
from sccp.errors import GameError

EVENT_TAG: Final = "sccp match"
RULES_TAG: Final = "chess"
TIME_CONTROL_TAG: Final = "600+0"
"""The historical 10+0 default for standalone rendering.

:func:`play_game` overrides it with the token's declared ``TimeControl``; the
transcript layer repeats that from the certificate.
"""

MoveHook = Callable[[int, chess.Move, str], None]
"""Called after every pushed move with ``(ply, move, san)``."""

ResignHook = Callable[[chess.Board, chess.Color], bool]
"""Called with ``(board, side_to_move)``; return True if that side resigns."""


@dataclass(frozen=True, slots=True)
class GameHooks:
    """Optional observers for the move loop."""

    on_move: MoveHook | None = None
    should_resign: ResignHook | None = None


@dataclass(frozen=True, slots=True)
class RawGame:
    """A finished (or stopped) game before any certificate is attached."""

    san_moves: tuple[str, ...]
    pgn: str
    result: GameResult
    termination: Termination
    final_board: chess.Board
    start_fen: str = chess.STARTING_FEN

    @property
    def plies(self) -> int:
        """Number of half-moves played."""
        return len(self.san_moves)


def _push_san(board: chess.Board, san: str, san_moves: list[str], source: str) -> chess.Move:
    """Apply one SAN move, raising :class:`GameError` when it is not legal."""
    try:
        move = board.parse_san(san)
    except ValueError as exc:
        raise GameError(
            f"{source} move {san!r} is illegal at ply {board.ply() + 1}: {exc}"
        ) from None
    san_moves.append(board.san(move))
    board.push(move)
    return move


def render_pgn(
    board: chess.Board,
    *,
    result: GameResult,
    termination: Termination,
    white: Engine,
    black: Engine,
    headers: Mapping[str, str] | None = None,
    start_fen: str | None = None,
    variant: str | None = None,
    time_control: str = TIME_CONTROL_TAG,
) -> str:
    """Render a finished board as PGN, with the sccp tags filled in.

    ``variant`` defaults to the board's own flavour for backwards compatibility.
    A variant whose rules need a start position carries ``SetUp``, ``FEN`` and
    the ruleset's canonical ``Variant`` header. ``time_control`` is the token's
    declared ``TimeControl`` tag, not a protocol-wide constant.
    """
    game = chess.pgn.Game.from_board(board)
    if variant is None:
        variant = CHESS960_VARIANT if board.chess960 else STANDARD_VARIANT
    rules = ruleset_for(variant)
    merged: dict[str, str] = {
        "Event": EVENT_TAG,
        "Site": "?",
        "Date": "????.??.??",
        "Round": "-",
        "White": white.name,
        "Black": black.name,
        "Rules": rules.pgn_rules,
        "TimeControl": time_control,
        "Termination": termination.value,
        "EngineWhite": white.fingerprint,
        "EngineBlack": black.fingerprint,
    }
    if headers is not None:
        merged.update(headers)
    merged["Result"] = result.value
    if rules.requires_start_fen:
        canonical = normalise_start_fen(start_fen, variant)
        merged["FEN"] = canonical
        merged["SetUp"] = "1"
        if rules.pgn_variant_header is not None:
            merged["Variant"] = rules.pgn_variant_header
    for tag, value in merged.items():
        game.headers[tag] = value
    return str(game)


def play_game(
    white: Engine,
    black: Engine,
    *,
    params: NetworkParams,
    opening: OpeningLine | Sequence[str] | None = None,
    limit: chess.engine.Limit | None = None,
    hooks: GameHooks | None = None,
    headers: Mapping[str, str] | None = None,
    variant: str = STANDARD_VARIANT,
    start_fen: str | None = None,
) -> RawGame:
    """Play one game under ``params`` and return a :class:`RawGame`.

    Stops at the first board outcome, when a hook resigns, or at
    ``params.max_plies`` (reported as ``Termination.MAX_PLIES`` with a ``*``
    result, which :func:`sccp.chess.pgn.validate_pgn` refuses to call a proof).

    ``limit`` controls engine effort; the default is
    ``chess.engine.Limit(nodes=params.nodes_per_move)``. Chess960 games must
    pass their ``start_fen``; the engine must advertise ``UCI_Chess960``
    (python-chess sets the option from the board before every position).
    """
    effort = limit if limit is not None else chess.engine.Limit(nodes=params.nodes_per_move)
    canonical_start = normalise_start_fen(start_fen, variant)
    board = board_for_variant(variant, canonical_start)
    variant_params = params.variant(variant)
    time_control = (
        f"{variant_params.time_control_initial_ms // 1000}"
        f"+{variant_params.time_control_increment_ms // 1000}"
    )
    san_moves: list[str] = []
    opening_moves = opening.moves if isinstance(opening, OpeningLine) else tuple(opening or ())

    result = GameResult.UNFINISHED
    termination = Termination.UNFINISHED
    resigned = False

    def notify(move: chess.Move, san: str) -> bool:
        """Fire the hooks; return True when the side to move resigns."""
        if hooks is not None and hooks.on_move is not None:
            hooks.on_move(board.ply(), move, san)
        return bool(
            hooks is not None
            and hooks.should_resign is not None
            and hooks.should_resign(board, board.turn)
        )

    def resignation() -> tuple[GameResult, Termination]:
        winner = GameResult.BLACK if board.turn == chess.WHITE else GameResult.WHITE
        return winner, Termination.RESIGNATION

    for san in opening_moves:
        move = _push_san(board, san, san_moves, "opening")
        if notify(move, san_moves[-1]):
            result, termination = resignation()
            resigned = True
            break

    while not resigned:
        outcome = board.outcome(claim_draw=False)
        if outcome is not None:
            result, termination = outcome_result(outcome)
            break
        if board.ply() >= params.max_plies:
            termination = Termination.MAX_PLIES
            result = GameResult.UNFINISHED
            break

        engine = white if board.turn == chess.WHITE else black
        move = engine.choose_move(board, effort)
        if not board.is_legal(move):
            raise GameError(
                f"{engine.name} proposed illegal move {move.uci()} at ply {board.ply() + 1}"
            )
        san_moves.append(board.san(move))
        board.push(move)

        if notify(move, san_moves[-1]):
            result, termination = resignation()
            break

    return RawGame(
        san_moves=tuple(san_moves),
        pgn=render_pgn(
            board,
            result=result,
            termination=termination,
            white=white,
            black=black,
            headers=headers,
            start_fen=canonical_start,
            variant=variant,
            time_control=time_control,
        ),
        result=result,
        termination=termination,
        final_board=board,
        start_fen=canonical_start,
    )
