"""Every way a PGN can fail, with the typed error that names it."""

from __future__ import annotations

import io
from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import Any

import chess
import chess.pgn
import pytest

from sccp.chess.pgn import GameExpectations, validate_pgn
from sccp.config import TESTNET
from sccp.errors import PgnValidationError

FOOLS_MATE = "1. f3 e5 2. g4 Qh4# 0-1"


def _tags(**overrides: str) -> Callable[[Mapping[str, str]], dict[str, str]]:
    def apply(base: Mapping[str, str]) -> dict[str, str]:
        return {**base, **overrides}

    return apply


def test_illegal_san_is_reported(
    make_pgn: Callable[[str, Mapping[str, str]], str], match_tags: dict[str, str]
) -> None:
    with pytest.raises(PgnValidationError, match="parse errors"):
        validate_pgn(make_pgn("1. e4 e5 2. Ke3 1-0", match_tags), params=TESTNET)


def test_illegal_castling_is_reported(
    make_pgn: Callable[[str, Mapping[str, str]], str], match_tags: dict[str, str]
) -> None:
    with pytest.raises(PgnValidationError, match="parse errors"):
        validate_pgn(make_pgn("1. O-O 1-0", match_tags), params=TESTNET)


def test_silently_dropped_token_is_caught_by_the_board(
    make_pgn: Callable[[str, Mapping[str, str]], str], match_tags: dict[str, str]
) -> None:
    """python-chess drops ``Qh9`` without an error; the terminal check catches it."""
    pgn = make_pgn("1. e4 e5 2. Nf3 Qh9 1-0", match_tags)
    with pytest.raises(PgnValidationError, match="no terminal position"):
        validate_pgn(pgn, params=TESTNET)


def test_multiple_games_are_rejected(
    make_pgn: Callable[[str, Mapping[str, str]], str], match_tags: dict[str, str]
) -> None:
    pgn = make_pgn("1. f3 e5 2. g4 Qh4# 0-1\n\n1. e4 e5 1-0", match_tags)
    with pytest.raises(PgnValidationError, match="more than one game"):
        validate_pgn(pgn, params=TESTNET)


def test_missing_and_blank_tags_are_rejected(
    make_pgn: Callable[[str, Mapping[str, str]], str], match_tags: dict[str, str]
) -> None:
    missing = _tags()(match_tags)
    del missing["MatchID"]
    with pytest.raises(PgnValidationError, match="missing required PGN tag: MatchID"):
        validate_pgn(make_pgn(FOOLS_MATE, missing), params=TESTNET)

    blank = _tags(Epoch="  ")(match_tags)
    with pytest.raises(PgnValidationError, match="missing required PGN tag: Epoch"):
        validate_pgn(make_pgn(FOOLS_MATE, blank), params=TESTNET)


def test_rules_tag_must_be_chess(
    make_pgn: Callable[[str, Mapping[str, str]], str], match_tags: dict[str, str]
) -> None:
    tags = _tags(Rules="chess960")(match_tags)
    with pytest.raises(PgnValidationError, match="unsupported Rules"):
        validate_pgn(make_pgn(FOOLS_MATE, tags), params=TESTNET)


def test_unterminated_result_is_rejected(
    make_pgn: Callable[[str, Mapping[str, str]], str], match_tags: dict[str, str]
) -> None:
    tags = _tags(Result="*")(match_tags)
    with pytest.raises(PgnValidationError, match="Result '\\*'"):
        validate_pgn(make_pgn("1. e4 e5 *", tags), params=TESTNET)


def test_result_header_must_match_the_board(
    make_pgn: Callable[[str, Mapping[str, str]], str], match_tags: dict[str, str]
) -> None:
    tags = _tags(Result="1-0")(match_tags)
    with pytest.raises(PgnValidationError, match="Result header"):
        validate_pgn(make_pgn(FOOLS_MATE, tags), params=TESTNET)


def test_termination_header_must_match_the_board(
    make_pgn: Callable[[str, Mapping[str, str]], str], match_tags: dict[str, str]
) -> None:
    tags = _tags(Termination="draw", Result="1/2-1/2")(match_tags)
    with pytest.raises(PgnValidationError, match="Termination header"):
        validate_pgn(make_pgn(FOOLS_MATE, tags), params=TESTNET)


def test_resignation_must_be_decisive(
    make_pgn: Callable[[str, Mapping[str, str]], str], match_tags: dict[str, str]
) -> None:
    tags = _tags(Result="1/2-1/2", Termination="resignation")(match_tags)
    with pytest.raises(PgnValidationError, match="decisive"):
        validate_pgn(make_pgn("1. e4 e5 1/2-1/2", tags), params=TESTNET)


def test_resignation_result_must_name_the_winner(
    make_pgn: Callable[[str, Mapping[str, str]], str], match_tags: dict[str, str]
) -> None:
    # After 1. e4 e5 it is White to move; a White resignation is 0-1, not 1-0.
    tags = _tags(Result="1-0", Termination="resignation")(match_tags)
    with pytest.raises(PgnValidationError, match="resigning side"):
        validate_pgn(make_pgn("1. e4 e5 1-0", tags), params=TESTNET)


def test_gameplay_only_terminations_are_not_proofs(
    make_pgn: Callable[[str, Mapping[str, str]], str], match_tags: dict[str, str]
) -> None:
    for termination in ("max-plies", "unfinished"):
        tags = _tags(Termination=termination, Result="1-0")(match_tags)
        with pytest.raises(PgnValidationError, match="not valid in a proof"):
            validate_pgn(make_pgn("1. e4 e5 1-0", tags), params=TESTNET)


def test_unknown_termination_is_rejected(
    make_pgn: Callable[[str, Mapping[str, str]], str], match_tags: dict[str, str]
) -> None:
    tags = _tags(Termination="time-forfeit")(match_tags)
    with pytest.raises(PgnValidationError, match="invalid Termination"):
        validate_pgn(make_pgn(FOOLS_MATE, tags), params=TESTNET)


@pytest.mark.parametrize("value", ["two", "-1", ""])
def test_numeric_tags_must_be_non_negative_integers(
    make_pgn: Callable[[str, Mapping[str, str]], str],
    match_tags: dict[str, str],
    value: str,
) -> None:
    tags = _tags(Epoch=value)(match_tags)
    with pytest.raises(PgnValidationError, match="Epoch"):
        validate_pgn(make_pgn(FOOLS_MATE, tags), params=TESTNET)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("match_id", "m-2"),
        ("epoch", 3),
        ("round_number", 1),
        ("seat_white", "sccpt1other"),
        ("seat_black", "sccpt1other"),
        ("engine_white", "mock-v1:9999999999999999"),
        ("engine_black", "mock-v1:9999999999999999"),
        ("opening_id", 7),
        ("opening_hash", "00" * 32),
    ],
)
def test_certificate_mismatches_are_rejected(
    make_pgn: Callable[[str, Mapping[str, str]], str],
    match_tags: dict[str, str],
    expectations: GameExpectations,
    field: str,
    value: object,
) -> None:
    with pytest.raises(PgnValidationError, match="certificate"):
        validate_pgn(
            make_pgn(FOOLS_MATE, match_tags),
            params=TESTNET,
            expected=replace(expectations, **{field: value}),
        )


def test_custom_start_position_is_rejected(
    make_pgn: Callable[[str, Mapping[str, str]], str], match_tags: dict[str, str]
) -> None:
    tags = {**match_tags, "SetUp": "1", "FEN": "4k3/8/8/8/8/8/8/4K3 w - - 0 1"}
    with pytest.raises(PgnValidationError, match="custom start positions"):
        validate_pgn(make_pgn("1. Kd2 1-0", tags), params=TESTNET)


def test_variations_are_only_accepted_for_offline_verification(
    make_pgn: Callable[[str, Mapping[str, str]], str],
    match_tags: dict[str, str],
    expectations: GameExpectations,
) -> None:
    pgn = make_pgn("1. f3 e5 (1... c5) 2. g4 Qh4# 0-1", match_tags)

    with pytest.raises(PgnValidationError, match="variations"):
        validate_pgn(pgn, params=TESTNET, expected=expectations)

    parsed = validate_pgn(pgn, params=TESTNET)
    assert parsed.san_moves == ("f3", "e5", "g4", "Qh4#")
    assert (
        parsed.game_hash == validate_pgn(make_pgn(FOOLS_MATE, match_tags), params=TESTNET).game_hash
    )


def test_parser_exceptions_become_validation_errors(
    make_pgn: Callable[[str, Mapping[str, str]], str],
    match_tags: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def explode(handle: io.StringIO) -> None:
        raise ValueError("tokenizer exploded")

    monkeypatch.setattr(chess.pgn, "read_game", explode)
    with pytest.raises(PgnValidationError, match="cannot parse PGN"):
        validate_pgn(make_pgn(FOOLS_MATE, match_tags), params=TESTNET)


def test_replay_exceptions_become_validation_errors(
    make_pgn: Callable[[str, Mapping[str, str]], str],
    match_tags: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def explode(self: chess.pgn.Game) -> Any:
        yield chess.Move.from_uci("f2f3")
        raise RuntimeError("move iterator exploded")

    monkeypatch.setattr(chess.pgn.Game, "mainline_moves", explode)
    with pytest.raises(PgnValidationError, match="cannot replay PGN"):
        validate_pgn(make_pgn(FOOLS_MATE, match_tags), params=TESTNET)


def test_replay_legality_gate_rejects_a_move_the_parser_kept(
    make_pgn: Callable[[str, Mapping[str, str]], str],
    match_tags: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``read_game`` can hand back a move the board will not accept."""

    def moves(self: chess.pgn.Game) -> Any:
        yield chess.Move.from_uci("e2e5")

    monkeypatch.setattr(chess.pgn.Game, "mainline_moves", moves)
    with pytest.raises(PgnValidationError, match="illegal move at ply 1"):
        validate_pgn(make_pgn(FOOLS_MATE, match_tags), params=TESTNET)


# --- python-chess gotchas from PROTOCOL.md section 3 -------------------------


def test_gotcha_read_game_returns_none_for_empty_input() -> None:
    assert chess.pgn.read_game(io.StringIO("")) is None
    assert chess.pgn.read_game(io.StringIO("\n\n")) is None
    header_only = chess.pgn.read_game(io.StringIO('[Event "x"]\n'))
    assert header_only is not None
    assert not list(header_only.mainline_moves())


def test_gotcha_read_game_consumes_one_game_per_call() -> None:
    handle = io.StringIO("1. e4 1-0\n\n1. d4 0-1\n")
    assert chess.pgn.read_game(handle) is not None
    assert chess.pgn.read_game(handle) is not None
    assert chess.pgn.read_game(handle) is None


def test_gotcha_board_returns_the_root_position() -> None:
    game = chess.pgn.read_game(io.StringIO("1. e4 e5 1-0"))
    assert game is not None
    board = game.board()
    assert board.ply() == 0
    assert board.fen() == chess.STARTING_FEN


def test_gotcha_push_accepts_an_illegal_move_but_the_validator_does_not() -> None:
    illegal = chess.Move.from_uci("e2e5")
    board = chess.Board()
    board.push(illegal)  # python-chess does not validate; our replay uses is_legal
    assert board.ply() == 1

    board = chess.Board()
    assert not board.is_legal(illegal)


def test_gotcha_san_is_relative_to_the_current_board() -> None:
    board = chess.Board()
    board.push_san("e4")
    board.push_san("e5")
    assert board.variation_san([]) == ""
    assert board.san(chess.Move.from_uci("g1f3")) == "Nf3"
