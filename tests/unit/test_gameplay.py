"""The move loop: determinism, legality, resignation and the max-ply guard."""

from __future__ import annotations

from dataclasses import dataclass

import chess
import chess.engine
import pytest

from sccp.chess.engines import MockEngine, ScriptedEngine
from sccp.chess.gameplay import GameHooks, play_game, render_pgn
from sccp.chess.openings import OpeningLine, load_book
from sccp.chess.pgn import GameExpectations, GameResult, Termination, validate_pgn
from sccp.config import DEVNET, TESTNET, NetworkParams
from sccp.errors import EngineError, GameError, PgnValidationError

LIMIT = chess.engine.Limit(nodes=50)


def _headers(line: OpeningLine) -> dict[str, str]:
    return {
        "MatchID": "m-1",
        "Epoch": "2",
        "RoundNumber": "0",
        "SeatWhite": "sccpt1white",
        "SeatBlack": "sccpt1black",
        "OpeningID": str(line.line_id),
        "OpeningHash": line.opening_hash,
    }


def _expectations(line: OpeningLine, white: MockEngine, black: MockEngine) -> GameExpectations:
    return GameExpectations(
        match_id="m-1",
        epoch=2,
        round_number=0,
        seat_white="sccpt1white",
        seat_black="sccpt1black",
        engine_white=white.fingerprint,
        engine_black=black.fingerprint,
        opening_id=line.line_id,
        opening_hash=line.opening_hash,
        opening_moves=line.moves,
    )


@dataclass(frozen=True)
class BadEngine:
    """An adapter that ignores legality, to prove the runner is the gate."""

    name: str = "bad"
    fingerprint: str = "bad:deadbeef"

    def choose_move(self, board: chess.Board, limit: chess.engine.Limit) -> chess.Move:
        return chess.Move.from_uci("e2e5")

    def close(self) -> None:
        """Nothing to release."""


def test_mock_self_play_is_deterministic_and_validates() -> None:
    line = load_book("v1").line(0)
    first_white, first_black = MockEngine(seed=14), MockEngine(seed=1014)
    second_white, second_black = MockEngine(seed=14), MockEngine(seed=1014)
    headers = _headers(line)

    first = play_game(
        first_white, first_black, params=DEVNET, opening=line, limit=LIMIT, headers=headers
    )
    second = play_game(
        second_white, second_black, params=DEVNET, opening=line, limit=LIMIT, headers=headers
    )

    assert first.san_moves == second.san_moves
    assert first.pgn == second.pgn
    assert first.san_moves[: line.plies] == line.moves
    assert first.plies <= DEVNET.max_plies
    assert first.result is not GameResult.UNFINISHED

    parsed = validate_pgn(
        first.pgn, params=DEVNET, expected=_expectations(line, first_white, first_black)
    )
    assert parsed.san_moves == first.san_moves
    assert parsed.result is first.result
    assert parsed.termination is first.termination


def test_max_plies_stops_the_game() -> None:
    params = NetworkParams.model_validate({**TESTNET.model_dump(), "min_plies": 1, "max_plies": 4})
    line = load_book("v1").line(0)
    game = play_game(
        MockEngine(seed=1),
        MockEngine(seed=2),
        params=params,
        limit=LIMIT,
        headers=_headers(line),
    )
    assert game.plies == 4
    assert game.termination is Termination.MAX_PLIES
    assert game.result is GameResult.UNFINISHED
    assert 'Result "*"' in game.pgn
    with pytest.raises(PgnValidationError, match="Result"):
        validate_pgn(game.pgn, params=params)


def test_resignation_hook_ends_the_game() -> None:
    calls: list[chess.Color] = []

    def resign(board: chess.Board, side: chess.Color) -> bool:
        calls.append(side)
        return len(calls) == 1

    game = play_game(
        MockEngine(seed=3),
        MockEngine(seed=4),
        params=DEVNET,
        opening=OpeningLine("v1", 0, "single move", ("e4",)),
        limit=LIMIT,
        hooks=GameHooks(should_resign=resign),
    )
    assert game.termination is Termination.RESIGNATION
    assert game.result is GameResult.WHITE  # Black resigned after White's e4
    assert game.plies == 1
    assert calls == [chess.BLACK]


def test_move_hook_sees_every_ply() -> None:
    seen: list[tuple[int, str]] = []

    def on_move(ply: int, move: chess.Move, san: str) -> None:
        seen.append((ply, san))

    game = play_game(
        MockEngine(seed=5),
        MockEngine(seed=6),
        params=DEVNET,
        opening=OpeningLine("v1", 0, "single move", ("e4",)),
        limit=chess.engine.Limit(nodes=10),
        hooks=GameHooks(on_move=on_move),
    )
    assert len(seen) == game.plies
    assert seen[0] == (1, "e4")
    assert [ply for ply, _ in seen] == list(range(1, game.plies + 1))


def test_scripted_fools_mate_derives_the_outcome() -> None:
    white = ScriptedEngine(["f3", "g4"], name="white")
    black = ScriptedEngine(["e5", "Qh4#"], name="black")
    line = OpeningLine("v1", 0, "empty", ())
    game = play_game(
        white,
        black,
        params=DEVNET,
        limit=LIMIT,
        headers=_headers(line),
    )
    assert game.san_moves == ("f3", "e5", "g4", "Qh4#")
    assert game.result is GameResult.BLACK
    assert game.termination is Termination.CHECKMATE
    assert 'Result "0-1"' in game.pgn

    expectations = GameExpectations(
        match_id="m-1",
        epoch=2,
        round_number=0,
        seat_white="sccpt1white",
        seat_black="sccpt1black",
        engine_white=white.fingerprint,
        engine_black=black.fingerprint,
        opening_id=0,
        opening_hash=line.opening_hash,
    )
    parsed = validate_pgn(game.pgn, params=DEVNET, expected=expectations)
    assert parsed.result is GameResult.BLACK


def test_illegal_opening_move_is_a_game_error() -> None:
    with pytest.raises(GameError, match="opening move"):
        play_game(
            MockEngine(),
            MockEngine(),
            params=DEVNET,
            opening=("e5",),
            limit=LIMIT,
        )


def test_illegal_engine_move_is_a_game_error() -> None:
    with pytest.raises(GameError, match="proposed illegal move"):
        play_game(BadEngine(), MockEngine(), params=DEVNET, limit=LIMIT)


def test_engine_exhaustion_propagates() -> None:
    with pytest.raises(EngineError, match="no scripted move left"):
        play_game(
            ScriptedEngine(["e4"]),
            ScriptedEngine([]),
            params=DEVNET,
            limit=LIMIT,
        )


def test_render_pgn_lets_headers_win() -> None:
    board = chess.Board()
    board.push_san("e4")
    pgn = render_pgn(
        board,
        result=GameResult.BLACK,
        termination=Termination.RESIGNATION,
        white=MockEngine(seed=1),
        black=MockEngine(seed=2),
        headers={"Event": "custom", "White": "override", "MatchID": "m-9"},
    )
    assert '[Event "custom"]' in pgn
    assert '[White "override"]' in pgn
    assert '[MatchID "m-9"]' in pgn
    assert '[Rules "chess"]' in pgn
    assert '[Termination "resignation"]' in pgn
    assert 'Result "0-1"' in pgn
    assert pgn.rstrip().endswith("0-1")


def test_resigned_game_pgn_validates() -> None:
    line = OpeningLine("v1", 0, "single move", ("e4",))
    game = play_game(
        MockEngine(seed=1),
        MockEngine(seed=2),
        params=DEVNET,
        opening=line,
        limit=LIMIT,
        hooks=GameHooks(should_resign=lambda board, side: board.ply() >= 4),
        headers=_headers(line),
    )
    assert game.termination is Termination.RESIGNATION
    parsed = validate_pgn(game.pgn, params=DEVNET)
    assert parsed.termination is Termination.RESIGNATION
    assert parsed.result is game.result
