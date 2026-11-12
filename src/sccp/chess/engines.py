"""Engine abstraction: UCI adapter, deterministic mocks (section 5.6).

Engines are deliberately dumb: ``choose_move`` proposes a move and the game
runner is the only component that decides whether it is legal. That keeps one
legality gate (``chess.Board.is_legal``) in the consensus path instead of
trusting every adapter.

``fingerprint`` values identify the mover in a match certificate. For a real
engine it is the SHA-256 of the binary, which an operator can reproduce with
``sha256sum``; for the mock and scripted engines it commits to their
configuration, so two different scripts never look alike.
"""

from __future__ import annotations

import contextlib
import random
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from types import MappingProxyType, TracebackType
from typing import Final, Literal, Protocol

import chess
import chess.engine

from sccp.errors import EngineError

MOCK_VERSION: Final = "mock-v1"
"""Bumped when the mock mover's distribution changes."""


class Engine(Protocol):
    """Anything that can propose a move for the side to move."""

    name: str

    @property
    def fingerprint(self) -> str:
        """Stable identity of this engine configuration."""
        ...

    def choose_move(self, board: chess.Board, limit: chess.engine.Limit) -> chess.Move:
        """Return a move for ``board.turn``. May be illegal; the runner checks."""
        ...

    def close(self) -> None:
        """Release engine resources. Must be idempotent."""
        ...


def engine_fingerprint(binary: Path) -> str:
    """SHA-256 of an engine binary as lowercase hex."""
    digest = sha256()
    try:
        with binary.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
    except OSError as exc:
        raise EngineError(f"cannot hash engine binary {binary}: {exc}") from None
    return digest.hexdigest()


@dataclass(slots=True)
class MockEngine:
    """A seeded random legal mover; identical seeds play identical games."""

    seed: int = 0
    name: str = "mock"
    _rng: random.Random = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._rng = random.Random(self.seed)

    @property
    def fingerprint(self) -> str:
        """Stable id of ``(version, seed, name)``."""
        digest = sha256(f"{MOCK_VERSION}:{self.seed}:{self.name}".encode()).hexdigest()
        return f"{MOCK_VERSION}:{digest[:16]}"

    def choose_move(self, board: chess.Board, limit: chess.engine.Limit) -> chess.Move:
        moves = list(board.legal_moves)
        if not moves:
            raise EngineError(f"{self.name} has no legal move in {board.fen()}")
        return self._rng.choice(moves)

    def close(self) -> None:
        """A mock engine holds no resources."""


class ScriptedEngine:
    """Plays a fixed list of moves (SAN or :class:`chess.Move`) in order."""

    def __init__(self, moves: Iterable[str | chess.Move], *, name: str = "scripted") -> None:
        self.name = name
        self._script: list[str | chess.Move] = list(moves)
        self._next = 0
        script_text = ",".join(
            move.uci() if isinstance(move, chess.Move) else move for move in self._script
        )
        self.fingerprint = f"scripted:{sha256(script_text.encode()).hexdigest()[:16]}"

    def choose_move(self, board: chess.Board, limit: chess.engine.Limit) -> chess.Move:
        if self._next >= len(self._script):
            raise EngineError(f"{self.name} has no scripted move left at ply {board.ply() + 1}")
        entry = self._script[self._next]
        self._next += 1
        if isinstance(entry, chess.Move):
            if not board.is_legal(entry):
                raise EngineError(
                    f"{self.name} scripted move {entry.uci()} is illegal at ply {board.ply() + 1}"
                )
            return entry
        try:
            return board.parse_san(entry)
        except ValueError as exc:
            raise EngineError(
                f"{self.name} scripted move {entry!r} is illegal at ply {board.ply() + 1}: {exc}"
            ) from None

    def close(self) -> None:
        """A scripted engine holds no resources."""


class UciEngine:
    """Thin wrapper over :class:`chess.engine.SimpleEngine`."""

    def __init__(
        self,
        engine: chess.engine.SimpleEngine,
        *,
        name: str,
        fingerprint: str,
        options: Mapping[str, chess.engine.ConfigValue] | None = None,
    ) -> None:
        self._engine = engine
        self._closed = False
        self.name = name
        self.fingerprint = fingerprint
        if options:
            try:
                engine.configure(dict(options))
            except chess.engine.EngineError as exc:
                engine.quit()
                raise EngineError(f"cannot configure {name}: {exc}") from None

    @classmethod
    def popen(
        cls,
        binary: Path,
        *,
        name: str | None = None,
        options: Mapping[str, chess.engine.ConfigValue] | None = None,
    ) -> UciEngine:
        """Start ``binary`` over UCI and record its fingerprint."""
        fingerprint = engine_fingerprint(binary)
        try:
            engine = chess.engine.SimpleEngine.popen_uci(str(binary))
        except (OSError, chess.engine.EngineError) as exc:
            raise EngineError(f"cannot start engine {binary}: {exc}") from None
        try:
            return cls(
                engine,
                name=name or binary.stem,
                fingerprint=fingerprint,
                options=options,
            )
        except EngineError:
            engine.quit()
            raise

    def choose_move(self, board: chess.Board, limit: chess.engine.Limit) -> chess.Move:
        try:
            result = self._engine.play(board, limit)
        except chess.engine.EngineError as exc:
            raise EngineError(f"{self.name} failed at ply {board.ply() + 1}: {exc}") from None
        if result.move is None:
            raise EngineError(f"{self.name} returned no move in {board.fen()}")
        return result.move

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(chess.engine.EngineError):  # already terminated
            self._engine.quit()

    def __enter__(self) -> UciEngine:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


EngineKind = Literal["uci", "mock"]

_DEFAULT_OPTIONS: Final[Mapping[str, chess.engine.ConfigValue]] = MappingProxyType(
    {"Threads": 1, "Hash": 16}
)


@dataclass(frozen=True, slots=True)
class EngineProfile:
    """A named engine configuration.

    ``kind`` says how to obtain the binary: ``mock`` needs none, ``uci`` needs
    ``--engine-path`` (or a binary fetched by ``scripts/fetch_engine.py``).
    """

    name: str
    kind: EngineKind = "uci"
    # ``default_factory`` rather than a ``MappingProxyType`` default: Python 3.11
    # rejects mappingproxy defaults as "mutable" (3.12+ allows them).
    options: Mapping[str, chess.engine.ConfigValue] = field(
        default_factory=lambda: _DEFAULT_OPTIONS
    )


ENGINE_PROFILES: Final[Mapping[str, EngineProfile]] = MappingProxyType(
    {
        "mock": EngineProfile(name="mock", kind="mock", options=MappingProxyType({})),
        "stockfish-19": EngineProfile(name="stockfish-19"),
        "stockfish-18": EngineProfile(name="stockfish-18"),
    }
)
"""Profiles pinned to binaries in ``data/engines.json``."""


def get_profile(profile: str | EngineProfile) -> EngineProfile:
    """Resolve a profile name, rejecting unknown ones."""
    if isinstance(profile, EngineProfile):
        return profile
    try:
        return ENGINE_PROFILES[profile]
    except KeyError:
        known = ", ".join(sorted(ENGINE_PROFILES))
        raise EngineError(f"unknown engine profile {profile!r}; known profiles: {known}") from None


def open_engine(
    profile: str | EngineProfile,
    *,
    binary: Path | None = None,
    seed: int = 0,
) -> Engine:
    """Build the engine described by ``profile``.

    ``mock`` ignores ``binary``; every other profile requires one and refuses to
    guess, because starting the wrong binary would silently change the
    certificate's fingerprint.
    """
    selected = get_profile(profile)
    if selected.kind == "mock":
        return MockEngine(seed=seed, name=selected.name)
    if binary is None:
        raise EngineError(
            f"engine profile {selected.name!r} needs an explicit binary; "
            f"pass an engine path or run scripts/fetch_engine.py"
        )
    return UciEngine.popen(binary, name=selected.name, options=selected.options)
