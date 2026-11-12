"""The pinned quality policy: blunders, resignation, cache and summary matching.

These tests use scripted engines, so the policy is exercised deterministically
without Stockfish. They cover the acceptance rules that a legal-but-weak game
fails and a resignation is only accepted when the position is lost.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

import chess
import chess.engine
import pytest

from sccp.chess.pgn import validate_pgn
from sccp.chess.quality import (
    QUALITY_V1,
    AnalysisCache,
    QualityProfile,
    analyse_game,
    analyse_position,
    engine_fingerprint_matches,
    get_profile,
    require_reference_engine,
    resignation_is_hopeless,
    summary_matches,
)
from sccp.config import TESTNET
from sccp.errors import QualityError

FOOLS_MATE = "1. f3 e5 2. g4 Qh4# 0-1"
STALEMATE = (
    "1. e3 a5 2. Qh5 Ra6 3. Qxa5 h5 4. Qxc7 Rah6 5. h4 f6 6. Qxd7+ Kf7 "
    "7. Qxb7 Qd3 8. Qxb8 Qh7 9. Qxc8 Kg6 10. Qe6 1/2-1/2"
)
RESIGNATION = "1. e4 e5 0-1"


def _profile(**overrides: object) -> QualityProfile:
    """A profile with the real policy id and deliberately simple thresholds."""
    base: dict[str, object] = {
        "policy": QUALITY_V1.policy,
        "engine": QUALITY_V1.engine,
        "depth": 1,
        "max_cpl": 100,
        "decided_cp": 10_000,
        "max_blunders_per_side": 0,
        "resign_cp": 300,
        "min_plies": 1,
    }
    return QualityProfile(**{**base, **overrides})  # type: ignore[arg-type]


class _ScriptedEngine:
    """A deterministic engine that scores every legal move from a table."""

    def __init__(
        self,
        score: Callable[[chess.Board, chess.Move], int] | int = 0,
        *,
        all_moves: bool = True,
        name: str = "scripted-quality",
        fingerprint: str = "scripted-quality-v1",
    ) -> None:
        self._score = score if callable(score) else (lambda board, move: score)
        self.all_moves = all_moves
        self.name = name
        self.fingerprint = fingerprint
        self.calls = 0

    def analyse(
        self,
        board: chess.Board,
        limit: chess.engine.Limit,
        *,
        multipv: int = 1,
    ) -> list[dict[str, object]]:
        self.calls += 1
        legal = list(board.legal_moves)
        if not legal:
            return [
                {
                    "score": chess.engine.PovScore(chess.engine.Mate(0), board.turn),
                    "pv": [],
                }
            ]
        scored = sorted(
            ((self._score(board, move), move) for move in legal),
            key=lambda item: -item[0],
        )
        if not self.all_moves:
            scored = scored[:1]
        return [
            {
                "score": chess.engine.PovScore(chess.engine.Cp(value), board.turn),
                "pv": [move],
            }
            for value, move in scored
        ]


class _RawEngine:
    """An engine that returns a fixed analysis shape."""

    name = "raw"
    fingerprint = "raw"

    def __init__(self, result: object) -> None:
        self.result = result

    def analyse(self, board: chess.Board, limit: chess.engine.Limit, *, multipv: int = 1):
        return self.result


class _ExplodingEngine:
    """An engine that must never be called."""

    name = "exploding"
    fingerprint = "exploding"

    def analyse(self, board: chess.Board, limit: chess.engine.Limit, *, multipv: int = 1):
        raise AssertionError("the policy must not analyse a finished position")


def _parsed(
    make_pgn: Callable[[str, Mapping[str, str]], str],
    match_tags: Mapping[str, str],
    moves: str,
    *,
    result: str,
    termination: str,
):
    tags = {**match_tags, "Result": result, "Termination": termination}
    return validate_pgn(make_pgn(moves, tags), params=TESTNET)


def _mate_report(make_pgn, match_tags, engine, profile=None):
    parsed = _parsed(make_pgn, match_tags, FOOLS_MATE, result="0-1", termination="checkmate")
    return analyse_game(parsed, engine, profile=profile or _profile())


def test_a_clean_game_passes_and_hashes_reproducibly(make_pgn, match_tags) -> None:
    report = _mate_report(make_pgn, match_tags, _ScriptedEngine())

    assert report.verdict == "pass"
    assert (report.white_blunders, report.black_blunders, report.worst_cpl) == (0, 0, 0)
    assert report.resignation_ok
    assert [move.seat for move in report.moves] == ["white", "black", "white", "black"]
    again = _mate_report(make_pgn, match_tags, _ScriptedEngine())
    assert again.trace_hash == report.trace_hash
    summary = report.summary()
    assert summary.trace_hash == report.trace_hash
    assert summary.verdict == "pass"


def test_a_weak_move_is_a_blunder_and_fails_the_policy(make_pgn, match_tags) -> None:
    start = chess.Board().fen()

    def score(board: chess.Board, move: chess.Move) -> int:
        return -600 if board.fen() == start and move.uci() == "f2f3" else 0

    report = _mate_report(make_pgn, match_tags, _ScriptedEngine(score))

    assert report.verdict == "fail"
    assert report.white_blunders == 1
    assert report.black_blunders == 0
    assert report.worst_cpl == 600
    assert report.moves[0].counted is True
    assert report.moves[0].played_cp == -600
    assert all(not move.counted for move in report.moves[1:])


def test_a_move_outside_multipv_is_scored_from_the_reply(make_pgn, match_tags) -> None:
    after_blunder = chess.Board()
    after_blunder.push_san("f3")
    reply_fen = after_blunder.fen()

    def score(board: chess.Board, move: chess.Move) -> int:
        return 600 if board.fen() == reply_fen else 0

    report = _mate_report(make_pgn, match_tags, _ScriptedEngine(score, all_moves=False))

    assert report.moves[0].san == "f3"
    assert report.moves[0].played_cp == -600
    assert report.moves[0].cpl == 600
    assert report.worst_cpl == 600
    assert report.verdict == "fail"


def test_decided_positions_are_not_counted_as_blunders(make_pgn, match_tags) -> None:
    start = chess.Board().fen()

    def score(board: chess.Board, move: chess.Move) -> int:
        if board.fen() != start:
            return 0
        return -2_000 if move.uci() == "f2f3" else -600

    report = _mate_report(make_pgn, match_tags, _ScriptedEngine(score), _profile(decided_cp=100))

    assert report.worst_cpl == 1_400
    assert report.moves[0].counted is False
    assert report.white_blunders == 0
    assert report.verdict == "pass"


def test_min_plies_fails_a_short_game(make_pgn, match_tags) -> None:
    report = _mate_report(make_pgn, match_tags, _ScriptedEngine(), _profile(min_plies=99))

    assert report.plies == 4
    assert report.verdict == "fail"


def test_checkmate_termination_requires_a_checkmate(make_pgn, match_tags) -> None:
    report = _mate_report(make_pgn, match_tags, _ScriptedEngine())

    assert report.resignation_ok is True
    assert report.moves[-1].san == "Qh4#"


def test_an_unjustified_resignation_fails(make_pgn, match_tags) -> None:
    parsed = _parsed(make_pgn, match_tags, RESIGNATION, result="0-1", termination="resignation")
    report = analyse_game(parsed, _ScriptedEngine(0), profile=_profile())

    assert parsed.termination.value == "resignation"
    assert report.resignation_ok is False
    assert report.verdict == "fail"


def test_a_lost_position_justifies_a_resignation(make_pgn, match_tags) -> None:
    parsed = _parsed(make_pgn, match_tags, RESIGNATION, result="0-1", termination="resignation")
    lost = parsed.final_board.fen()

    def score(board: chess.Board, move: chess.Move) -> int:
        return -400 if board.fen() == lost else 0

    report = analyse_game(parsed, _ScriptedEngine(score), profile=_profile())

    assert report.resignation_ok is True
    assert report.verdict == "pass"


def test_quality_analysis_rejects_a_draw(make_pgn, match_tags) -> None:
    parsed = _parsed(make_pgn, match_tags, STALEMATE, result="1/2-1/2", termination="draw")

    with pytest.raises(QualityError, match="decisive"):
        analyse_game(parsed, _ScriptedEngine(0), profile=_profile())


def test_resignation_is_hopeless_short_circuits_finished_boards() -> None:
    mate = chess.Board()
    for san in ("f3", "e5", "g4", "Qh4#"):
        mate.push_san(san)
    assert mate.is_checkmate()
    assert resignation_is_hopeless(mate, _ExplodingEngine())
    stalemate = chess.Board("7k/5Q2/6K1/8/8/8/8/8 b - - 0 1")
    assert stalemate.is_stalemate()
    assert not resignation_is_hopeless(stalemate, _ExplodingEngine())


def test_resignation_is_hopeless_uses_the_score_boundary() -> None:
    board = chess.Board()
    assert resignation_is_hopeless(board, _ScriptedEngine(-300), profile=_profile())
    assert not resignation_is_hopeless(board, _ScriptedEngine(-299), profile=_profile())
    assert not resignation_is_hopeless(board, _ScriptedEngine(0), profile=_profile())


def test_analysis_cache_is_bounded_and_lru() -> None:
    with pytest.raises(QualityError, match="at least one entry"):
        AnalysisCache(max_entries=0)

    cache = AnalysisCache(max_entries=2)
    first: tuple[int, int, str] = (1, 1, "a")
    second: tuple[int, int, str] = (1, 1, "b")
    third: tuple[int, int, str] = (1, 1, "c")
    cache.put(first, ())
    cache.put(second, ())
    assert cache.get(first) == ()  # refresh ``first``
    cache.put(third, ())
    assert len(cache) == 2
    assert cache.get(first) == ()
    assert cache.get(second) is None
    assert cache.get(third) == ()


class _CountingEngine(_ScriptedEngine):
    """A scripted engine that records every position it is asked about."""

    def __init__(self) -> None:
        super().__init__()
        self.seen: list[str] = []

    def analyse(self, board: chess.Board, limit: chess.engine.Limit, *, multipv: int = 1):
        self.seen.append(board.fen())
        return super().analyse(board, limit, multipv=multipv)


def test_analyse_position_uses_the_cache() -> None:
    engine = _CountingEngine()
    cache = AnalysisCache()
    board = chess.Board()
    first = analyse_position(engine, board, depth=3, cache=cache)
    second = analyse_position(engine, board, depth=3, cache=cache)

    assert first == second
    assert engine.calls == 1
    analyse_position(engine, board, depth=4, cache=cache)
    assert engine.calls == 2
    analyse_position(engine, board, depth=4)
    analyse_position(engine, board, depth=4)
    assert engine.calls == 4


@pytest.mark.parametrize(
    ("result", "message"),
    [
        ("not an analysis", "unsupported analysis shape"),
        ([], "no analysis lines"),
        ([{"pv": []}], "no usable score"),
        (
            [{"score": chess.engine.PovScore(chess.engine.Cp(0), chess.WHITE), "pv": ["e2e4"]}],
            "non-move",
        ),
        (
            [
                {
                    "score": chess.engine.PovScore(chess.engine.Cp(0), chess.WHITE),
                    "pv": "e2e4",
                }
            ],
            "must be a list",
        ),
    ],
)
def test_analysis_rejects_bad_engine_output(make_pgn, match_tags, result, message) -> None:
    with pytest.raises(QualityError, match=message):
        _mate_report(make_pgn, match_tags, _RawEngine(result))


def test_profiles_and_registry_validate() -> None:
    with pytest.raises(QualityError, match="policy id"):
        QualityProfile(policy="", engine="stockfish-17")
    with pytest.raises(QualityError, match="positive depth"):
        QualityProfile(policy="quality-v1", engine="stockfish-17", depth=0)
    with pytest.raises(QualityError, match="non-negative"):
        QualityProfile(policy="quality-v1", engine="stockfish-17", max_cpl=-1)
    with pytest.raises(QualityError, match="min_plies"):
        QualityProfile(policy="quality-v1", engine="stockfish-17", min_plies=0)


def test_get_profile_accepts_an_id_or_an_instance() -> None:
    assert get_profile("quality-v1") is QUALITY_V1
    assert get_profile(QUALITY_V1) is QUALITY_V1
    with pytest.raises(QualityError, match="unknown quality profile"):
        get_profile("no-such-profile")


def test_profile_as_dict_sorts_options() -> None:
    profile = _profile(options={"Threads": 1, "Hash": 16})
    document = profile.as_dict()

    assert document["options"] == {"Hash": 16, "Threads": 1}
    assert document["policy"] == QUALITY_V1.policy


def test_engine_fingerprint_matching() -> None:
    digest = "ab" * 32
    assert engine_fingerprint_matches(
        _profile(binary_sha256=digest), _ScriptedEngine(fingerprint=digest)
    )
    assert not engine_fingerprint_matches(
        _profile(binary_sha256="00" * 32), _ScriptedEngine(fingerprint=digest)
    )
    assert engine_fingerprint_matches(_profile(), _ScriptedEngine(name="Stockfish 17"))
    assert not engine_fingerprint_matches(_profile(), _ScriptedEngine(name="Fruit 2.1"))
    assert engine_fingerprint_matches(
        _profile(), _ScriptedEngine(name="", fingerprint="stockfish-17")
    )


def test_require_reference_engine_enforces_the_profile() -> None:
    require_reference_engine(_profile(), _ScriptedEngine(name="Stockfish 17"))
    with pytest.raises(QualityError, match="does not match quality profile"):
        require_reference_engine(_profile(), _ScriptedEngine(name="Fruit 2.1"))


def test_summary_matching_accepts_an_identical_summary(make_pgn, match_tags) -> None:
    report = _mate_report(make_pgn, match_tags, _ScriptedEngine())
    summary = report.summary()

    assert summary_matches(summary, report) == (True, "summary agrees with analysis")
    bare = summary.model_copy(update={"trace_hash": None, "engine_fingerprint": None})
    assert summary_matches(bare, report)[0] is True


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("policy", "other-policy", "policy is"),
        ("verdict", "fail", "verdict is"),
        ("white_blunders", 3, "white blunders are"),
        ("black_blunders", 3, "black blunders are"),
        ("worst_cpl", 999, "worst CPL is"),
        ("trace_hash", "00" * 32, "trace hash does not match"),
        ("engine_fingerprint", "other-engine", "engine fingerprint does not match"),
    ],
)
def test_summary_matching_rejects_each_mismatch(
    make_pgn, match_tags, field, value, message
) -> None:
    report = _mate_report(make_pgn, match_tags, _ScriptedEngine())
    summary = report.summary().model_copy(update={field: value})

    agrees, reason = summary_matches(summary, report)

    assert agrees is False
    assert message in reason


def test_summary_matching_rejects_a_missing_summary(make_pgn, match_tags) -> None:
    report = _mate_report(make_pgn, match_tags, _ScriptedEngine())

    assert summary_matches(None, report) == (False, "proof carries no quality summary")
