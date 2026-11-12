"""Opening book loading, validation and seeded selection (section 5.6).

The book lives in ``data/openings.json`` at the repository root and is copied
into the wheel as ``sccp/chess/data/openings.json`` at build time. Every line is
replayed on load, so a malformed or illegal book fails at import/selection time
rather than during a consensus check.

Selection is a pure function of the beacon and the match id:

``index = SHA256(beacon || "opening" || match_id)[:8] % len(book)``

``line_id`` is the line's index in its book version, which is what
``opening_hash = H("sccp/v1/opening", {book_version, line_id, moves})`` commits
to.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from functools import cache
from hashlib import sha256
from pathlib import Path
from types import MappingProxyType
from typing import Final

import chess

from sccp.config import NetworkParams
from sccp.errors import OpeningBookError
from sccp.serialization import Domain, domain_hex

MAX_BOOK_PLIES: Final = 12
"""No book line may be longer than this (section 5.6)."""

BEACON_BYTES: Final = 32
"""Beacons are 32-byte digests, so selection rejects anything else."""

BOOK_FORMAT: Final = 1

PACKAGED_BOOK_PATH: Final[Path] = Path(__file__).resolve().parent / "data" / "openings.json"
REPOSITORY_BOOK_PATH: Final[Path] = Path(__file__).resolve().parents[3] / "data" / "openings.json"


@dataclass(frozen=True, slots=True)
class OpeningLine:
    """One book line: a stable id, a human name and a legal SAN prefix."""

    book_version: str
    line_id: int
    name: str
    moves: tuple[str, ...]

    @property
    def plies(self) -> int:
        """Number of book half-moves."""
        return len(self.moves)

    @property
    def opening_hash(self) -> str:
        """``H("sccp/v1/opening", {book_version, line_id, moves})`` as hex."""
        return domain_hex(
            Domain.OPENING,
            {
                "book_version": self.book_version,
                "line_id": self.line_id,
                "moves": list(self.moves),
            },
        )


@dataclass(frozen=True, slots=True)
class OpeningBook:
    """All validated lines of one book version."""

    version: str
    lines: tuple[OpeningLine, ...]

    def __len__(self) -> int:
        return len(self.lines)

    def __iter__(self) -> Iterator[OpeningLine]:
        return iter(self.lines)

    def line(self, line_id: int) -> OpeningLine:
        """Return the line whose stable id is ``line_id``."""
        if not 0 <= line_id < len(self.lines):
            raise OpeningBookError(
                f"book {self.version!r} has no line {line_id} (it has {len(self.lines)} lines)"
            )
        line = self.lines[line_id]
        if line.line_id != line_id:  # pragma: no cover - construction guarantees it
            raise OpeningBookError(f"book {self.version!r} is internally inconsistent")
        return line

    def select(self, beacon: bytes, match_id: str) -> OpeningLine:
        """Pick the line for a match, deterministically and uniformly."""
        if len(beacon) != BEACON_BYTES:
            raise OpeningBookError(f"beacon must be {BEACON_BYTES} bytes, got {len(beacon)}")
        if not self.lines:  # pragma: no cover - loading rejects empty books
            raise OpeningBookError(f"book {self.version!r} is empty")
        digest = sha256(beacon + b"opening" + match_id.encode("utf-8")).digest()
        index = int.from_bytes(digest[:8], "big") % len(self.lines)
        return self.lines[index]


def _check_legal(line: OpeningLine) -> tuple[str, ...]:
    """Replay a line, returning the canonical SAN and rejecting illegal moves."""
    board = chess.Board()
    canonical: list[str] = []
    for number, san in enumerate(line.moves, start=1):
        if board.is_game_over(claim_draw=False):
            raise OpeningBookError(
                f"book {line.book_version!r} line {line.line_id} continues after the game ended"
            )
        try:
            move = board.parse_san(san)
        except ValueError:
            raise OpeningBookError(
                f"book {line.book_version!r} line {line.line_id} has illegal move "
                f"{san!r} at ply {number}"
            ) from None
        canonical.append(board.san(move))
        board.push(move)
    return tuple(canonical)


def _parse_line(version: str, index: int, raw: object) -> OpeningLine:
    """Validate one JSON line object and replay it."""
    if not isinstance(raw, dict):
        raise OpeningBookError(f"book {version!r} line {index} is not an object")
    fields: Mapping[object, object] = raw
    name = fields.get("name")
    moves = fields.get("moves")
    if not isinstance(name, str) or not name.strip():
        raise OpeningBookError(f"book {version!r} line {index} needs a non-empty name")
    if not isinstance(moves, list) or not moves:
        raise OpeningBookError(f"book {version!r} line {index} needs a non-empty move list")
    if not all(isinstance(move, str) and move for move in moves):
        raise OpeningBookError(f"book {version!r} line {index} has a non-string move")
    if len(moves) > MAX_BOOK_PLIES:
        raise OpeningBookError(
            f"book {version!r} line {index} has {len(moves)} plies; the cap is {MAX_BOOK_PLIES}"
        )
    line = OpeningLine(
        book_version=version,
        line_id=index,
        name=name.strip(),
        moves=tuple(str(move) for move in moves),
    )
    return OpeningLine(
        book_version=version,
        line_id=index,
        name=line.name,
        moves=_check_legal(line),
    )


def _parse_book(version: str, raw: object) -> OpeningBook:
    """Validate one book object."""
    if not isinstance(raw, dict):
        raise OpeningBookError(f"book {version!r} is not an object")
    fields: Mapping[object, object] = raw
    raw_lines = fields.get("lines")
    if not isinstance(raw_lines, list) or not raw_lines:
        raise OpeningBookError(f"book {version!r} needs at least one line")
    lines = tuple(_parse_line(version, index, item) for index, item in enumerate(raw_lines))
    names = [line.name for line in lines]
    if len(set(names)) != len(names):
        raise OpeningBookError(f"book {version!r} has duplicate line names")
    return OpeningBook(version=version, lines=lines)


def books_from_document(document: object) -> Mapping[str, OpeningBook]:
    """Validate a decoded ``openings.json`` document into books.

    Raising here rather than at selection time keeps a broken book out of the
    consensus path entirely.
    """
    if not isinstance(document, dict):
        raise OpeningBookError("opening book document must be a JSON object")
    fields: Mapping[object, object] = document
    if fields.get("format") != BOOK_FORMAT:
        raise OpeningBookError(
            f"unsupported opening book format {fields.get('format')!r}; expected {BOOK_FORMAT}"
        )
    raw_books = fields.get("books")
    if not isinstance(raw_books, dict) or not raw_books:
        raise OpeningBookError("opening book document needs a non-empty 'books' object")
    books: dict[str, OpeningBook] = {}
    for version, raw_book in raw_books.items():
        if not isinstance(version, str) or not version.strip():
            raise OpeningBookError("book versions must be non-empty strings")
        books[version] = _parse_book(version, raw_book)
    return MappingProxyType(books)


def default_book_path() -> Path:
    """The packaged book when installed, else the repository copy."""
    for candidate in (PACKAGED_BOOK_PATH, REPOSITORY_BOOK_PATH):
        if candidate.is_file():
            return candidate
    raise OpeningBookError(
        f"opening book not found; looked in {PACKAGED_BOOK_PATH} and {REPOSITORY_BOOK_PATH}"
    )


@cache
def _load_books_cached(path: Path) -> Mapping[str, OpeningBook]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise OpeningBookError(f"cannot read opening book {path}: {exc}") from None
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise OpeningBookError(f"opening book {path} is not valid JSON: {exc}") from None
    return books_from_document(document)


def load_books(path: Path | None = None) -> Mapping[str, OpeningBook]:
    """Load and validate every book version, caching by resolved path."""
    resolved = (path or default_book_path()).expanduser().resolve()
    return _load_books_cached(resolved)


def load_book(version: str, path: Path | None = None) -> OpeningBook:
    """Load one book version by name."""
    books = load_books(path)
    try:
        return books[version]
    except KeyError:
        known = ", ".join(sorted(books))
        raise OpeningBookError(f"unknown opening book {version!r}; known books: {known}") from None


def book_for(params: NetworkParams, path: Path | None = None) -> OpeningBook:
    """The book selected by ``params.book_version``."""
    return load_book(params.book_version, path)
