"""The non-consensus quality policy.

Quality analysis asks a stronger question than legality: did both sides actually
play strong chess? Every move is compared against a pinned reference engine at a
fixed depth and a bounded search, and a game fails when either side exceeds its
blunder budget or when a resignation is not justified by the position.

The policy is deliberately *not* consensus-critical. Engine evaluations can vary
by a centipawn between CPUs and builds, so block validity never depends on this
module. Instead, proofs carry a compact :class:`QualitySummary`, validators
sample them deterministically, and a bonded challenge is the enforcement path.

All arithmetic here is integer centipawns and integer plies. A report is a pure
function of the validated PGN and the engine output, which is what makes
``trace_hash`` reproducible on a different machine running the same pinned
profile.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

import chess
import chess.engine

from sccp.chess.pgn import GameResult, ParsedGame, Termination
from sccp.errors import QualityError
from sccp.protocol.models import Color, QualitySummary, Verdict
from sccp.serialization import Domain, domain_hex

MATE_SCORE: int = 100_000
"""Centipawn proxy for mate, matching the protocol scoring convention."""

DEFAULT_CACHE_ENTRIES: int = 4_096
"""How many position analyses :class:`AnalysisCache` keeps before eviction."""


@dataclass(frozen=True, slots=True)
class QualityProfile:
    """A versioned reference-engine profile (PROTOCOL.md section 9.2)."""

    policy: str
    engine: str
    depth: int = 14
    max_cpl: int = 150
    decided_cp: int = 600
    max_blunders_per_side: int = 2
    resign_cp: int = 300
    min_plies: int = 30
    options: Mapping[str, int | str | bool] = field(default_factory=dict)
    binary_sha256: str | None = None
    net_sha256: str | None = None
    syzygy: str | None = None

    def __post_init__(self) -> None:
        if not self.policy or self.depth < 1:
            raise QualityError("a quality profile needs a policy id and a positive depth")
        if self.max_cpl < 0 or self.decided_cp < 0 or self.max_blunders_per_side < 0:
            raise QualityError("quality thresholds must be non-negative")
        if self.min_plies < 1:
            raise QualityError("a quality profile needs min_plies >= 1")

    def as_dict(self) -> dict[str, object]:
        """Canonical mapping for a policy announcement or a trace."""
        return {
            "policy": self.policy,
            "engine": self.engine,
            "depth": self.depth,
            "max_cpl": self.max_cpl,
            "decided_cp": self.decided_cp,
            "max_blunders_per_side": self.max_blunders_per_side,
            "resign_cp": self.resign_cp,
            "min_plies": self.min_plies,
            "options": dict(sorted(self.options.items())),
            "binary_sha256": self.binary_sha256,
            "net_sha256": self.net_sha256,
            "syzygy": self.syzygy,
        }


QUALITY_V1 = QualityProfile(
    policy="quality-v1",
    engine="stockfish-17",
    depth=14,
    max_cpl=150,
    decided_cp=600,
    max_blunders_per_side=2,
    resign_cp=300,
    min_plies=30,
    options={"Threads": 1, "Hash": 16, "MultiPV": 5, "Contempt": 0},
)
"""The mainnet reference profile from PROTOCOL.md section 9.2."""

REFERENCE_PROFILES: Mapping[str, QualityProfile] = {"quality-v1": QUALITY_V1}
"""Every pinned profile, keyed by its policy id."""


def get_profile(profile: str | QualityProfile) -> QualityProfile:
    """Resolve a profile id, rejecting an unknown one instead of guessing."""
    if isinstance(profile, QualityProfile):
        return profile
    try:
        return REFERENCE_PROFILES[profile]
    except KeyError:
        known = ", ".join(sorted(REFERENCE_PROFILES))
        raise QualityError(
            f"unknown quality profile {profile!r}; known profiles: {known}"
        ) from None


AnalysisResult = chess.engine.InfoDict | list[chess.engine.InfoDict]


@runtime_checkable
class AnalysisEngine(Protocol):
    """The analysis subset of an engine that quality checking needs."""

    name: str

    @property
    def fingerprint(self) -> str:
        """Stable identity of this engine configuration."""
        ...

    def analyse(
        self,
        board: chess.Board,
        limit: chess.engine.Limit,
        *,
        multipv: int = 1,
    ) -> AnalysisResult:
        """Return one or more scored principal variations for ``board``."""
        ...


@dataclass(frozen=True, slots=True)
class MoveAssessment:
    """One analysed ply."""

    ply: int
    seat: Color
    san: str
    uci: str
    best_uci: str
    best_cp: int
    played_cp: int
    cpl: int
    counted: bool

    def as_dict(self) -> dict[str, object]:
        """Canonical trace entry."""
        return {
            "ply": self.ply,
            "seat": self.seat,
            "san": self.san,
            "uci": self.uci,
            "best_uci": self.best_uci,
            "best_cp": self.best_cp,
            "played_cp": self.played_cp,
            "cpl": self.cpl,
            "counted": self.counted,
        }


@dataclass(frozen=True, slots=True)
class QualityReport:
    """The full analysis of one game, plus its committed summary."""

    profile: QualityProfile
    engine_fingerprint: str
    engine_name: str
    game_hash: str
    verdict: Verdict
    white_blunders: int
    black_blunders: int
    worst_cpl: int
    resignation_ok: bool
    plies: int
    moves: tuple[MoveAssessment, ...]

    @property
    def trace_hash(self) -> str:
        """``H("sccp/v1/quality", trace)`` as lowercase hex."""
        return domain_hex(Domain.QUALITY, self.as_dict())

    def as_dict(self) -> dict[str, object]:
        """The full trace, retrievable by ``trace_hash``."""
        return {
            "policy": self.profile.policy,
            "profile": self.profile.as_dict(),
            "engine_name": self.engine_name,
            "engine_fingerprint": self.engine_fingerprint,
            "game_hash": self.game_hash,
            "verdict": self.verdict,
            "white_blunders": self.white_blunders,
            "black_blunders": self.black_blunders,
            "worst_cpl": self.worst_cpl,
            "resignation_ok": self.resignation_ok,
            "plies": self.plies,
            "moves": [move.as_dict() for move in self.moves],
        }

    def summary(self) -> QualitySummary:
        """The compact object a proof carries on the wire."""
        return QualitySummary(
            policy=self.profile.policy,
            verdict=self.verdict,
            trace_hash=self.trace_hash,
            engine_fingerprint=self.engine_fingerprint,
            white_blunders=self.white_blunders,
            black_blunders=self.black_blunders,
            worst_cpl=self.worst_cpl,
        )


class AnalysisCache:
    """A bounded, deterministic position-analysis cache.

    A referenced position can occur on several plies, especially in a repeated
    opening, so caching both the pre-move analysis and the reply analysis halves
    the work on a long game. Eviction is least-recently-used and therefore
    independent of timing or hash randomisation: identical runs evict identical
    entries.
    """

    def __init__(self, max_entries: int = DEFAULT_CACHE_ENTRIES) -> None:
        if max_entries < 1:
            raise QualityError("an analysis cache needs at least one entry")
        self.max_entries = max_entries
        self._entries: OrderedDict[tuple[int, int, str], tuple[chess.engine.InfoDict, ...]] = (
            OrderedDict()
        )

    def __len__(self) -> int:
        """Number of cached positions."""
        return len(self._entries)

    def get(self, key: tuple[int, int, str]) -> tuple[chess.engine.InfoDict, ...] | None:
        """Return cached lines for ``key`` and mark them as recently used."""
        try:
            value = self._entries.pop(key)
        except KeyError:
            return None
        self._entries[key] = value
        return value

    def put(self, key: tuple[int, int, str], lines: tuple[chess.engine.InfoDict, ...]) -> None:
        """Store ``lines``, evicting the oldest entry when full."""
        self._entries.pop(key, None)
        self._entries[key] = lines
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)


def _pov_cp(line: Mapping[str, object], color: chess.Color) -> int:
    """The line's score from ``color``'s point of view, in centipawns."""
    score = line.get("score")
    if not isinstance(score, chess.engine.PovScore):
        raise QualityError("quality analysis needs a scored principal variation")
    value = score.pov(color).score(mate_score=MATE_SCORE)
    if value is None:
        raise QualityError("quality analysis returned a mate score with no distance")
    return int(value)


def _pv(line: Mapping[str, object]) -> tuple[chess.Move, ...]:
    """The principal variation of one engine line, validated."""
    raw = line.get("pv", ())
    if not isinstance(raw, (list, tuple)):
        raise QualityError("engine line 'pv' must be a list of moves")
    moves: list[chess.Move] = []
    for move in raw:
        if not isinstance(move, chess.Move):
            raise QualityError("engine line 'pv' contains a non-move")
        moves.append(move)
    return tuple(moves)


def analyse_position(
    engine: AnalysisEngine,
    board: chess.Board,
    *,
    depth: int,
    multipv: int = 1,
    cache: AnalysisCache | None = None,
) -> tuple[chess.engine.InfoDict, ...]:
    """Analyse ``board`` once, using ``cache`` when supplied."""
    key = (depth, multipv, board.fen())
    if cache is not None:
        cached = cache.get(key)
        if cached is not None:
            return cached
    try:
        result = engine.analyse(
            board.copy(),
            chess.engine.Limit(depth=depth),
            multipv=multipv,
        )
    except chess.engine.EngineError as exc:  # pragma: no cover - UciEngine wraps this
        raise QualityError(f"reference engine failed: {exc}") from None
    if isinstance(result, dict):
        lines: tuple[chess.engine.InfoDict, ...] = (result,)
    elif isinstance(result, list) and all(isinstance(line, dict) for line in result):
        lines = tuple(result)
    else:
        raise QualityError("reference engine returned an unsupported analysis shape")
    if not lines:
        raise QualityError("reference engine returned no analysis lines")
    if cache is not None:
        cache.put(key, lines)
    return lines


def _best_line(
    lines: tuple[chess.engine.InfoDict, ...],
    board: chess.Board,
) -> tuple[chess.engine.InfoDict, int]:
    """The strongest line and its score from the side to move's point of view."""
    for line in lines:
        try:
            return line, _pov_cp(line, board.turn)
        except QualityError:
            continue
    raise QualityError("reference engine returned no usable score")


def _played_score(
    lines: tuple[chess.engine.InfoDict, ...],
    move: chess.Move,
    board: chess.Board,
) -> int | None:
    """The score of ``move`` when the engine's MultiPV includes it."""
    for line in lines:
        pv = _pv(line)
        if pv and pv[0] == move:
            try:
                return _pov_cp(line, board.turn)
            except QualityError:
                continue
    return None


def _fallback_played_score(
    engine: AnalysisEngine,
    board: chess.Board,
    move: chess.Move,
    *,
    depth: int,
    cache: AnalysisCache | None,
) -> int:
    """Score a move the engine did not list by analysing the reply position."""
    board.push(move)
    try:
        reply_lines = analyse_position(engine, board, depth=depth, multipv=1, cache=cache)
        _, reply_cp = _best_line(reply_lines, board)
        return -reply_cp
    finally:
        board.pop()


def resignation_is_hopeless(
    board: chess.Board,
    engine: AnalysisEngine,
    *,
    profile: QualityProfile = QUALITY_V1,
    cache: AnalysisCache | None = None,
) -> bool:
    """True when the side to move is lost enough to justify resigning.

    A checkmate is always hopeless. Otherwise the reference engine must score the
    position at or below ``-resign_cp`` for the side that resigned (the side to
    move in the final position, by PGN convention).
    """
    if board.is_checkmate():
        return True
    if board.is_game_over():
        return False
    lines = analyse_position(engine, board, depth=profile.depth, multipv=1, cache=cache)
    _, score = _best_line(lines, board)
    return score <= -profile.resign_cp


def analyse_game(
    parsed: ParsedGame,
    engine: AnalysisEngine,
    *,
    profile: QualityProfile = QUALITY_V1,
    cache: AnalysisCache | None = None,
) -> QualityReport:
    """Run the quality policy over an already validated PGN.

    Every move is compared with the reference engine. A move outside the top
    ``MultiPV`` lines is scored by analysing the position after it and negating
    the opponent's score, which is the same comparison without paying for an
    unbounded MultiPV.
    """
    if parsed.result is GameResult.DRAW or not parsed.result.is_decisive:
        raise QualityError("quality analysis needs a decisive game")
    if parsed.termination is Termination.DRAW:
        raise QualityError("quality analysis needs a decisive termination")

    board = chess.Board(parsed.start_fen)
    assessments: list[MoveAssessment] = []
    white_blunders = 0
    black_blunders = 0
    worst_cpl = 0

    for ply, san in enumerate(parsed.san_moves, start=1):
        try:
            move = board.parse_san(san)
        except ValueError as exc:  # pragma: no cover - ParsedGame already checked
            raise QualityError(f"validated PGN move {san!r} does not replay: {exc}") from None
        seat: Color = "white" if board.turn == chess.WHITE else "black"
        lines = analyse_position(
            engine,
            board,
            depth=profile.depth,
            multipv=max(1, int(profile.options.get("MultiPV", 5))),
            cache=cache,
        )
        best, best_cp = _best_line(lines, board)
        best_pv = _pv(best)
        best_uci = best_pv[0].uci() if best_pv else move.uci()
        played_cp = _played_score(lines, move, board)
        if played_cp is None:
            played_cp = _fallback_played_score(
                engine, board, move, depth=profile.depth, cache=cache
            )
        cpl = max(0, best_cp - played_cp)
        counted = abs(best_cp) < profile.decided_cp and cpl > profile.max_cpl
        if counted:
            if seat == "white":
                white_blunders += 1
            else:
                black_blunders += 1
        worst_cpl = max(worst_cpl, cpl)
        assessments.append(
            MoveAssessment(
                ply=ply,
                seat=seat,
                san=san,
                uci=move.uci(),
                best_uci=best_uci,
                best_cp=best_cp,
                played_cp=played_cp,
                cpl=cpl,
                counted=counted,
            )
        )
        board.push(move)

    if board.fen() != parsed.final_board.fen():
        raise QualityError("analysed position diverged from the validated final position")

    resignation_ok = True
    if parsed.termination is Termination.RESIGNATION:
        resignation_ok = resignation_is_hopeless(board, engine, profile=profile, cache=cache)
    elif parsed.termination is Termination.CHECKMATE:
        resignation_ok = board.is_checkmate()

    blunders_ok = (
        white_blunders <= profile.max_blunders_per_side
        and black_blunders <= profile.max_blunders_per_side
    )
    verdict: Verdict = (
        "pass" if blunders_ok and resignation_ok and parsed.plies >= profile.min_plies else "fail"
    )
    return QualityReport(
        profile=profile,
        engine_fingerprint=getattr(engine, "fingerprint", ""),
        engine_name=getattr(engine, "name", type(engine).__name__),
        game_hash=parsed.game_hash,
        verdict=verdict,
        white_blunders=white_blunders,
        black_blunders=black_blunders,
        worst_cpl=worst_cpl,
        resignation_ok=resignation_ok,
        plies=parsed.plies,
        moves=tuple(assessments),
    )


analyze_game = analyse_game
"""American-spelling alias for callers that expect ``analyze_game``."""


def engine_fingerprint_matches(profile: QualityProfile, engine: AnalysisEngine) -> bool:
    """Whether ``engine`` looks like the engine pinned by ``profile``.

    Binary fingerprints are SHA-256 digests, so a profile that carries one can be
    checked exactly. A profile without a digest falls back to its human-readable
    engine name, normalised so ``"stockfish-17"`` matches ``"Stockfish 17"``.
    """
    actual_fingerprint = getattr(engine, "fingerprint", "")
    if profile.binary_sha256 and actual_fingerprint:
        return profile.binary_sha256 == actual_fingerprint
    expected = "".join(character for character in profile.engine.lower() if character.isalnum())
    actual_name = getattr(engine, "name", "")
    actual = "".join(character for character in actual_name.lower() if character.isalnum())
    if not actual:
        actual = "".join(
            character for character in str(actual_fingerprint).lower() if character.isalnum()
        )
    return bool(expected) and expected in actual


def summary_matches(
    summary: QualitySummary | None,
    report: QualityReport,
) -> tuple[bool, str]:
    """Compare a proof's summary with a re-run report.

    Returns ``(agrees, reason)``. This is used by the sampler and by adjudication;
    it is never called during block validation, because a disagreement is an
    economic event rather than a consensus error.
    """
    if summary is None:
        return False, "proof carries no quality summary"
    if summary.policy != report.profile.policy:
        return False, f"policy is {summary.policy!r}, expected {report.profile.policy!r}"
    if summary.verdict != report.verdict:
        return False, f"verdict is {summary.verdict!r}, analysis says {report.verdict!r}"
    if summary.white_blunders != report.white_blunders:
        return False, (
            f"white blunders are {summary.white_blunders}, analysis says {report.white_blunders}"
        )
    if summary.black_blunders != report.black_blunders:
        return False, (
            f"black blunders are {summary.black_blunders}, analysis says {report.black_blunders}"
        )
    if summary.worst_cpl != report.worst_cpl:
        return False, f"worst CPL is {summary.worst_cpl}, analysis says {report.worst_cpl}"
    if summary.trace_hash is not None and summary.trace_hash != report.trace_hash:
        return False, "trace hash does not match the re-run trace"
    if (
        summary.engine_fingerprint is not None
        and report.engine_fingerprint
        and summary.engine_fingerprint != report.engine_fingerprint
    ):
        return False, "engine fingerprint does not match the re-run engine"
    return True, "summary agrees with analysis"


def require_reference_engine(profile: QualityProfile, engine: AnalysisEngine) -> None:
    """Raise :class:`QualityError` when ``engine`` is not the pinned reference."""
    if not engine_fingerprint_matches(profile, engine):
        raise QualityError(
            f"engine {getattr(engine, 'name', type(engine).__name__)!r} does not match "
            f"quality profile {profile.policy!r} ({profile.engine})"
        )


__all__ = [
    "DEFAULT_CACHE_ENTRIES",
    "MATE_SCORE",
    "QUALITY_V1",
    "REFERENCE_PROFILES",
    "AnalysisCache",
    "AnalysisEngine",
    "AnalysisResult",
    "MoveAssessment",
    "QualityProfile",
    "QualityReport",
    "analyse_game",
    "analyse_position",
    "analyze_game",
    "engine_fingerprint_matches",
    "get_profile",
    "require_reference_engine",
    "resignation_is_hopeless",
    "summary_matches",
]
