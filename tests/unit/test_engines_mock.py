"""Engines: deterministic mocks, scripts, fingerprints and the UCI adapter."""

from __future__ import annotations

from collections.abc import Sequence
from hashlib import sha256
from pathlib import Path
from typing import Any

import chess
import chess.engine
import pytest

from sccp.chess.engines import (
    ENGINE_PROFILES,
    EngineProfile,
    MockEngine,
    ScriptedEngine,
    UciEngine,
    engine_fingerprint,
    get_profile,
    open_engine,
)
from sccp.errors import EngineError

LIMIT = chess.engine.Limit(nodes=100)


def _play(engine: Any, plies: int = 12) -> list[str]:
    board = chess.Board()
    moves: list[str] = []
    for _ in range(plies):
        move = engine.choose_move(board, LIMIT)
        assert board.is_legal(move)
        moves.append(board.san(move))
        board.push(move)
    return moves


def test_mock_engine_is_deterministic_per_seed() -> None:
    assert _play(MockEngine(seed=7)) == _play(MockEngine(seed=7))
    assert _play(MockEngine(seed=7)) != _play(MockEngine(seed=8))


def test_mock_engine_identity() -> None:
    one = MockEngine(seed=1)
    two = MockEngine(seed=2)
    assert one.name == "mock"
    assert one.fingerprint.startswith("mock-v1:")
    assert one.fingerprint == MockEngine(seed=1).fingerprint
    assert one.fingerprint != two.fingerprint
    assert one.fingerprint != MockEngine(seed=1, name="other").fingerprint


def test_mock_engine_refuses_a_finished_board() -> None:
    stalemate = chess.Board("7k/5Q2/6K1/8/8/8/8/8 b - - 0 1")
    with pytest.raises(EngineError, match="no legal move"):
        MockEngine().choose_move(stalemate, LIMIT)


def test_mock_engine_close_is_idempotent() -> None:
    engine = MockEngine()
    engine.close()
    engine.close()


def test_scripted_engine_plays_san_and_moves() -> None:
    engine = ScriptedEngine(["e4", chess.Move.from_uci("e7e5"), "Nf3"])
    assert _play(engine, plies=3) == ["e4", "e5", "Nf3"]
    engine.close()


def test_scripted_engine_runs_out_of_moves() -> None:
    engine = ScriptedEngine(["e4"])
    assert _play(engine, plies=1) == ["e4"]
    with pytest.raises(EngineError, match="no scripted move left"):
        engine.choose_move(chess.Board(), LIMIT)


def test_scripted_engine_rejects_illegal_entries() -> None:
    san_engine = ScriptedEngine(["Ke3"])
    with pytest.raises(EngineError, match="is illegal"):
        san_engine.choose_move(chess.Board(), LIMIT)

    move_engine = ScriptedEngine([chess.Move.from_uci("e2e5")])
    with pytest.raises(EngineError, match="is illegal"):
        move_engine.choose_move(chess.Board(), LIMIT)


def test_scripted_engine_fingerprint() -> None:
    first = ScriptedEngine(["e4", "e5"])
    again = ScriptedEngine(["e4", "e5"])
    other = ScriptedEngine(["d4", "d5"])
    assert first.fingerprint == again.fingerprint
    assert first.fingerprint != other.fingerprint
    assert first.fingerprint.startswith("scripted:")


def test_engine_fingerprint_hashes_the_file(tmp_path: Path) -> None:
    binary = tmp_path / "engine"
    binary.write_bytes(b"not really an engine")
    assert engine_fingerprint(binary) == sha256(b"not really an engine").hexdigest()
    with pytest.raises(EngineError, match="cannot hash"):
        engine_fingerprint(tmp_path / "missing")


def test_profiles() -> None:
    assert set(ENGINE_PROFILES) == {"mock", "stockfish-19", "stockfish-18"}
    assert get_profile("mock").kind == "mock"
    custom = EngineProfile(name="custom", kind="mock")
    assert get_profile(custom) is custom
    with pytest.raises(EngineError, match="unknown engine profile"):
        get_profile("stockfish-99")


def test_open_engine_needs_a_binary_for_uci_profiles(tmp_path: Path) -> None:
    mock = open_engine("mock", seed=3)
    assert isinstance(mock, MockEngine)
    with pytest.raises(EngineError, match="needs an explicit binary"):
        open_engine("stockfish-19")
    with pytest.raises(EngineError, match="cannot hash"):
        open_engine("stockfish-19", binary=tmp_path / "missing")


class FakeSimpleEngine:
    """Duck-typed stand-in for ``chess.engine.SimpleEngine``."""

    def __init__(
        self,
        move: chess.Move | None = None,
        *,
        play_error: Exception | None = None,
        configure_error: Exception | None = None,
    ) -> None:
        self.move = move
        self.play_error = play_error
        self.configure_error = configure_error
        self.configured: dict[str, Any] | None = None
        self.quit_calls = 0

    def play(self, board: chess.Board, limit: chess.engine.Limit) -> chess.engine.PlayResult:
        if self.play_error is not None:
            raise self.play_error
        return chess.engine.PlayResult(self.move, None)

    def configure(self, options: dict[str, Any]) -> None:
        if self.configure_error is not None:
            raise self.configure_error
        self.configured = dict(options)

    def quit(self) -> None:
        self.quit_calls += 1


def _uci(fake: FakeSimpleEngine, **options: Any) -> UciEngine:
    return UciEngine(fake, name="fake", fingerprint="f" * 64, options=options or None)


def test_uci_engine_wraps_play() -> None:
    move = chess.Move.from_uci("e2e4")
    fake = FakeSimpleEngine(move)
    engine = _uci(fake, Threads=1)
    assert fake.configured == {"Threads": 1}
    assert engine.choose_move(chess.Board(), LIMIT) == move


def test_uci_engine_reports_engine_failures() -> None:
    boom = FakeSimpleEngine(play_error=chess.engine.EngineError("crashed"))
    with pytest.raises(EngineError, match="failed at ply 1"):
        _uci(boom).choose_move(chess.Board(), LIMIT)

    silent = FakeSimpleEngine(None)
    with pytest.raises(EngineError, match="returned no move"):
        _uci(silent).choose_move(chess.Board(), LIMIT)


def test_uci_engine_closes_once_and_supports_context_management() -> None:
    fake = FakeSimpleEngine(chess.Move.from_uci("e2e4"))
    with _uci(fake) as engine:
        assert engine.name == "fake"
    engine.close()
    assert fake.quit_calls == 1


def test_uci_engine_configuration_failure_quits() -> None:
    fake = FakeSimpleEngine(configure_error=chess.engine.EngineError("bad option"))
    with pytest.raises(EngineError, match="cannot configure"):
        _uci(fake, Threads=2)
    assert fake.quit_calls >= 1


def test_popen_uses_the_binary_fingerprint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    binary = tmp_path / "stockfish"
    binary.write_bytes(b"engine bytes")
    fake = FakeSimpleEngine(chess.Move.from_uci("e2e4"))
    monkeypatch.setattr(chess.engine.SimpleEngine, "popen_uci", staticmethod(lambda path: fake))

    engine = UciEngine.popen(binary, options={"Threads": 1, "Hash": 16})
    assert engine.name == "stockfish"
    assert engine.fingerprint == sha256(b"engine bytes").hexdigest()
    assert fake.configured == {"Threads": 1, "Hash": 16}
    assert engine.choose_move(chess.Board(), LIMIT) == chess.Move.from_uci("e2e4")

    engine.close()
    assert fake.quit_calls == 1


def test_popen_wraps_start_failures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    binary = tmp_path / "stockfish"
    binary.write_bytes(b"engine bytes")

    def missing(path: str) -> Sequence[str]:
        raise OSError("no such binary")

    monkeypatch.setattr(chess.engine.SimpleEngine, "popen_uci", staticmethod(missing))
    with pytest.raises(EngineError, match="cannot start engine"):
        UciEngine.popen(binary)

    def broken(path: str) -> Sequence[str]:
        raise chess.engine.EngineError("bad handshake")

    monkeypatch.setattr(chess.engine.SimpleEngine, "popen_uci", staticmethod(broken))
    with pytest.raises(EngineError, match="cannot start engine"):
        UciEngine.popen(binary)


def test_popen_quits_when_configuration_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = tmp_path / "stockfish"
    binary.write_bytes(b"engine bytes")
    fake = FakeSimpleEngine(configure_error=chess.engine.EngineError("bad option"))
    monkeypatch.setattr(chess.engine.SimpleEngine, "popen_uci", staticmethod(lambda path: fake))
    with pytest.raises(EngineError, match="cannot configure"):
        UciEngine.popen(binary, options={"Threads": 2})
    assert fake.quit_calls >= 1
