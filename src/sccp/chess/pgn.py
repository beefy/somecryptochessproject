"""PGN validation and canonical game hashing (docs/IMPLEMENTATION_PLAN.md 5.8).

``validate_pgn`` turns untrusted PGN text into a :class:`ParsedGame` or raises
:class:`~sccp.errors.PgnValidationError`. It never mutates global state and has
no engine dependency, so every rule below is unit-testable.

The pipeline exists because ``chess.pgn.read_game`` is a permissive parser: it
stops at the first error it records, silently drops tokens it cannot interpret
(a trailing ``Qh9`` produces no error at all) and always supplies default
headers, including ``Result "*"``. Trusting it alone would let a proof claim a
result the board never reached.
"""

from __future__ import annotations

import io
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Final

import chess
import chess.pgn

from sccp.config import NetworkParams
from sccp.errors import PgnValidationError
from sccp.serialization import Domain, domain_hex

RULES_VALUE: Final = "chess"
"""The only supported value of the ``Rules`` tag in protocol version 1."""

REQUIRED_TAGS: Final[tuple[str, ...]] = (
    "Event",
    "Site",
    "Date",
    "Round",
    "White",
    "Black",
    "Result",
    "Termination",
    "MatchID",
    "Epoch",
    "RoundNumber",
    "SeatWhite",
    "SeatBlack",
    "EngineWhite",
    "EngineBlack",
    "OpeningID",
    "OpeningHash",
    "Rules",
)
"""Every tag a proof-grade PGN must carry (section 5.8, step 3)."""

_PROOF_TERMINATIONS: Final[frozenset[str]] = frozenset({"checkmate", "draw", "resignation"})


class GameResult(StrEnum):
    """The PGN result vocabulary. ``*`` exists for unfinished games only."""

    WHITE = "1-0"
    BLACK = "0-1"
    DRAW = "1/2-1/2"
    UNFINISHED = "*"

    @property
    def is_decisive(self) -> bool:
        """True for ``1-0`` and ``0-1``."""
        return self in (GameResult.WHITE, GameResult.BLACK)

    @classmethod
    def parse(cls, value: str) -> GameResult:
        """Parse a ``Result`` header, rejecting anything outside the vocabulary."""
        try:
            return cls(value)
        except ValueError:
            raise PgnValidationError(
                f"invalid PGN Result {value!r}; expected '1-0', '0-1' or '1/2-1/2'"
            ) from None


class Termination(StrEnum):
    """How a game ended.

    ``MAX_PLIES`` and ``UNFINISHED`` are produced by :mod:`sccp.chess.gameplay`
    when a live game is stopped; a proof may only carry the first three values.
    """

    CHECKMATE = "checkmate"
    DRAW = "draw"
    RESIGNATION = "resignation"
    MAX_PLIES = "max-plies"
    UNFINISHED = "unfinished"

    @classmethod
    def parse(cls, value: str) -> Termination:
        """Parse a ``Termination`` header."""
        try:
            return cls(value)
        except ValueError:
            known = ", ".join(sorted(_PROOF_TERMINATIONS))
            raise PgnValidationError(
                f"invalid Termination {value!r}; expected one of: {known}"
            ) from None


@dataclass(frozen=True, slots=True)
class GameExpectations:
    """The certificate fields a PGN must agree with (section 5.8, steps 1 and 4).

    M2's ``MatchCertificate`` builds one of these; M1 keeps the dependency out of
    :mod:`sccp.chess` so this module stays protocol-independent.
    """

    match_id: str
    epoch: int
    round_number: int
    seat_white: str
    seat_black: str
    engine_white: str
    engine_black: str
    opening_id: int
    opening_hash: str
    opening_moves: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ParsedGame:
    """A PGN that passed every validation step.

    ``final_board`` is the board after the last mainline move. Treat it as
    read-only; call :meth:`chess.Board.copy` before analysing it.
    """

    game_hash: str
    pgn_text: str
    san_moves: tuple[str, ...]
    final_board: chess.Board
    result: GameResult
    termination: Termination
    tags: Mapping[str, str]
    start_fen: str = chess.STARTING_FEN

    @property
    def plies(self) -> int:
        """Number of half-moves in the mainline."""
        return len(self.san_moves)

    @property
    def winner(self) -> GameResult | None:
        """``WHITE``/``BLACK`` for a decisive game, ``None`` for a draw."""
        return self.result if self.result.is_decisive else None


def outcome_result(outcome: chess.Outcome) -> tuple[GameResult, Termination]:
    """Map a non-null :class:`chess.Outcome` to ``(result, termination)``."""
    if outcome.termination is chess.Termination.CHECKMATE:
        if outcome.winner == chess.WHITE:
            return GameResult.WHITE, Termination.CHECKMATE
        if outcome.winner == chess.BLACK:
            return GameResult.BLACK, Termination.CHECKMATE
        raise PgnValidationError("checkmate without a winner")  # pragma: no cover
    return GameResult.DRAW, Termination.DRAW


def canonical_game_hash(
    network: str,
    san_moves: Sequence[str],
    result: GameResult | str,
    *,
    start_fen: str = chess.STARTING_FEN,
) -> str:
    """``H("sccp/v1/game", {network, start_fen, san_list, result})`` as hex.

    Headers, comments, clocks and formatting never enter the hash; two PGNs with
    the same moves and result on the same network hash identically.
    """
    result_value = GameResult(result).value
    return domain_hex(
        Domain.GAME,
        {
            "network": network,
            "start_fen": start_fen,
            "san_list": list(san_moves),
            "result": result_value,
        },
    )


def _decode_text(pgn_text: object) -> str:
    """Decode a PGN payload, rejecting non-UTF-8 bytes and non-text values."""
    if isinstance(pgn_text, bytes):
        try:
            return pgn_text.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PgnValidationError(f"PGN is not valid UTF-8: {exc}") from None
    if isinstance(pgn_text, str):
        return pgn_text
    raise PgnValidationError(f"PGN must be str or bytes, not {type(pgn_text).__name__}")


def _read_single_game(text: str) -> chess.pgn.Game:
    """Read exactly one game and reject a payload that holds a second one."""
    handle = io.StringIO(text)
    try:
        game = chess.pgn.read_game(handle)
        if game is None:
            raise PgnValidationError("empty PGN: no game found")
        if chess.pgn.read_game(handle) is not None:
            raise PgnValidationError("payload contains more than one game")
    except PgnValidationError:
        raise
    except (ValueError, RuntimeError) as exc:  # parser failures outside game.errors
        raise PgnValidationError(f"cannot parse PGN: {exc}") from None
    return game


def _require_tags(game: chess.pgn.Game) -> dict[str, str]:
    """Collect the headers and enforce the required-tag contract."""
    tags = dict(game.headers.items())
    for name in REQUIRED_TAGS:
        value = tags.get(name, "")
        if not value.strip():
            raise PgnValidationError(f"missing required PGN tag: {name}")
    if tags["Rules"] != RULES_VALUE:
        raise PgnValidationError(
            f"unsupported Rules tag {tags['Rules']!r}; expected {RULES_VALUE!r}"
        )
    return tags


def _int_tag(tags: Mapping[str, str], name: str) -> int:
    """Parse a non-negative integer tag, rejecting junk and negative values."""
    raw = tags[name]
    try:
        value = int(raw)
    except ValueError:
        raise PgnValidationError(f"tag {name} is not an integer: {raw!r}") from None
    if value < 0:
        raise PgnValidationError(f"tag {name} must not be negative: {value}")
    return value


def _check_expectations(
    tags: Mapping[str, str],
    expected: GameExpectations,
    numbers: Mapping[str, int],
) -> None:
    """Compare the PGN against the certificate it claims to belong to."""
    text_pairs = (
        ("MatchID", expected.match_id),
        ("SeatWhite", expected.seat_white),
        ("SeatBlack", expected.seat_black),
        ("EngineWhite", expected.engine_white),
        ("EngineBlack", expected.engine_black),
        ("OpeningHash", expected.opening_hash),
    )
    for name, wanted in text_pairs:
        actual = tags[name]
        if actual != wanted:
            raise PgnValidationError(
                f"tag {name} is {actual!r}, but the certificate says {wanted!r}"
            )
    number_pairs = (
        ("Epoch", expected.epoch),
        ("RoundNumber", expected.round_number),
        ("OpeningID", expected.opening_id),
    )
    for tag_name, wanted_number in number_pairs:
        actual_number = numbers[tag_name]
        if actual_number != wanted_number:
            raise PgnValidationError(
                f"tag {tag_name} is {actual_number}, but the certificate says {wanted_number}"
            )


def _has_variations(game: chess.pgn.Game) -> bool:
    """True when the mainline has a sibling move anywhere (a variation)."""
    node: chess.pgn.GameNode = game
    while node.variations:
        if len(node.variations) > 1:
            return True
        node = node.variations[0]
    return False


def _replay(game: chess.pgn.Game, params: NetworkParams) -> tuple[list[str], chess.Board]:
    """Replay the mainline, returning the canonical SAN list and final board.

    ``board.push`` happily accepts illegal moves, so legality is checked with
    ``is_legal`` before every push and SAN is generated from the live board.
    """
    board = chess.Board()
    san_moves: list[str] = []
    try:
        for move in game.mainline_moves():
            if board.ply() >= params.max_plies:
                raise PgnValidationError(f"game exceeds max_plies ({params.max_plies})")
            if not board.is_legal(move):
                raise PgnValidationError(f"illegal move at ply {board.ply() + 1}: {move.uci()}")
            san_moves.append(board.san(move))
            board.push(move)
    except PgnValidationError:
        raise
    except (ValueError, RuntimeError) as exc:
        raise PgnValidationError(f"cannot replay PGN: {exc}") from None
    return san_moves, board


def _check_outcome(
    board: chess.Board,
    result: GameResult,
    termination: Termination,
    resignation_check: Callable[[chess.Board], bool] | None,
) -> None:
    """Make the headers agree with the board, then the board agree with them."""
    outcome = board.outcome(claim_draw=False)
    if outcome is None:
        if termination is not Termination.RESIGNATION:
            raise PgnValidationError(
                "the board has no terminal position; only Termination 'resignation' is "
                f"allowed, got {termination.value!r}"
            )
        if not result.is_decisive:
            raise PgnValidationError("a resignation must have a decisive Result")
        if resignation_check is not None and not resignation_check(board):
            raise PgnValidationError(
                "resignation rejected: the quality policy does not consider the position lost"
            )
        # The side to move resigned, so the other side won.
        winner = GameResult.BLACK if board.turn == chess.WHITE else GameResult.WHITE
        if result is not winner:
            raise PgnValidationError(
                f"Result {result.value!r} does not match the resigning side "
                f"(expected {winner.value!r})"
            )
        return

    computed_result, computed_termination = outcome_result(outcome)
    if termination is not computed_termination:
        raise PgnValidationError(
            f"Termination header {termination.value!r} does not match the board "
            f"({computed_termination.value!r})"
        )
    if result is not computed_result:
        raise PgnValidationError(
            f"Result header {result.value!r} does not match the board ({computed_result.value!r})"
        )


def validate_pgn(
    pgn_text: str | bytes,
    *,
    params: NetworkParams,
    expected: GameExpectations | None = None,
    require_win: bool = False,
    resignation_check: Callable[[chess.Board], bool] | None = None,
) -> ParsedGame:
    """Validate untrusted PGN text and return a :class:`ParsedGame`.

    The steps mirror docs/IMPLEMENTATION_PLAN.md section 5.8: size and encoding,
    exactly one game, parser errors, required and certificate tags, legality of
    every move, the opening prefix, result/termination agreement, and -- for a
    proof -- a decisive result of at least ``params.min_plies``.

    ``resignation_check`` is the M5 quality-policy hook: it receives the final
    board and must return ``True`` when the resigning side is lost. It is only
    called for games without a board outcome.
    """
    text = _decode_text(pgn_text)
    size = len(text.encode("utf-8"))
    if size > params.max_pgn_bytes:
        raise PgnValidationError(
            f"PGN is {size} bytes; the cap for {params.network_id} is {params.max_pgn_bytes}"
        )
    if not text.strip():
        raise PgnValidationError("empty PGN: no game found")

    game = _read_single_game(text)
    if game.errors:
        details = "; ".join(str(error) for error in game.errors)
        raise PgnValidationError(f"PGN parse errors: {details}")

    start_fen = game.headers.get("FEN")
    if start_fen is not None and start_fen != chess.STARTING_FEN:
        raise PgnValidationError(
            "custom start positions (FEN/SetUp) are not supported in protocol v1"
        )

    tags = _require_tags(game)
    numbers = {name: _int_tag(tags, name) for name in ("Epoch", "RoundNumber", "OpeningID")}
    if expected is not None:
        if _has_variations(game):
            raise PgnValidationError("variations are not allowed in a proof")
        _check_expectations(tags, expected, numbers)

    result = GameResult.parse(tags["Result"])
    if result is GameResult.UNFINISHED:
        raise PgnValidationError("unterminated game: Result '*' cannot be proven")
    termination = Termination.parse(tags["Termination"])
    if termination.value not in _PROOF_TERMINATIONS:
        raise PgnValidationError(f"Termination {termination.value!r} is not valid in a proof")

    san_moves, final_board = _replay(game, params)

    if expected is not None and expected.opening_moves:
        prefix = tuple(san_moves[: len(expected.opening_moves)])
        if prefix != expected.opening_moves:
            raise PgnValidationError(
                f"game does not follow opening line {expected.opening_id}: "
                f"expected {list(expected.opening_moves)}, got {list(prefix)}"
            )

    _check_outcome(final_board, result, termination, resignation_check)

    if require_win:
        if not result.is_decisive:
            raise PgnValidationError("draws are valid PGNs, but a proof needs a win")
        if len(san_moves) < params.min_plies:
            raise PgnValidationError(
                f"game has {len(san_moves)} plies; a proof needs at least {params.min_plies}"
            )

    return ParsedGame(
        game_hash=canonical_game_hash(params.network_id, san_moves, result),
        pgn_text=text,
        san_moves=tuple(san_moves),
        final_board=final_board,
        result=result,
        termination=termination,
        tags=MappingProxyType(tags),
    )


def validate_pgn_file(
    path: Path,
    *,
    params: NetworkParams,
    expected: GameExpectations | None = None,
    require_win: bool = False,
    resignation_check: Callable[[chess.Board], bool] | None = None,
) -> ParsedGame:
    """Read ``path`` as UTF-8 bytes and hand it to :func:`validate_pgn`."""
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise PgnValidationError(f"cannot read PGN file {path}: {exc}") from None
    return validate_pgn(
        data,
        params=params,
        expected=expected,
        require_win=require_win,
        resignation_check=resignation_check,
    )
