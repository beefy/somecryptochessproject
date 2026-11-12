"""Opening book loading, validation and seeded selection (PROTOCOL.md section 7)."""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

import chess
import pytest

from sccp.chess.openings import (
    MAX_BOOK_PLIES,
    OpeningBook,
    OpeningLine,
    book_for,
    books_from_document,
    default_book_path,
    load_book,
    load_books,
)
from sccp.config import DEVNET, MAINNET, TESTNET
from sccp.errors import OpeningBookError


def _document(lines: object, *, version: str = "book") -> dict[str, Any]:
    return {"format": 1, "books": {version: {"lines": lines}}}


def _replay(moves: tuple[str, ...]) -> None:
    board = chess.Board()
    for san in moves:
        board.push(board.parse_san(san))


def test_repository_book_loads_and_is_shipped() -> None:
    path = default_book_path()
    assert path.is_file()
    books = load_books()
    assert {"v1", "dev-1"} <= set(books)
    book = books["v1"]
    assert len(book) >= 64
    assert book.line(0).line_id == 0
    assert [line.line_id for line in book] == list(range(len(book)))
    assert len({line.name for line in book}) == len(book)
    for line in book:
        assert 1 <= line.plies <= MAX_BOOK_PLIES
        _replay(line.moves)


def test_dev_book_matches_v1_lines_for_readability() -> None:
    books = load_books()
    assert [line.moves for line in books["dev-1"]] == [line.moves for line in books["v1"]]


def test_opening_hash_is_stable(
    chess_vectors: dict[str, Any],
) -> None:
    vector = chess_vectors["opening"]
    line = load_book(vector["book_version"]).line(vector["line_id"])
    assert line.moves == tuple(vector["moves"])
    assert line.name == vector["name"]
    assert line.opening_hash == vector["hash"]


def test_opening_hash_includes_the_book_version() -> None:
    v1 = load_book("v1").line(0)
    dev = load_book("dev-1").line(0)
    assert v1.moves == dev.moves
    assert v1.opening_hash != dev.opening_hash


def test_book_for_each_network() -> None:
    assert book_for(TESTNET).version == TESTNET.book_version
    assert book_for(DEVNET).version == DEVNET.book_version
    assert book_for(MAINNET).version == MAINNET.book_version


def test_selection_is_deterministic() -> None:
    book = load_book("v1")
    beacon = bytes(range(32))
    assert book.select(beacon, "m-1") is book.select(beacon, "m-1")
    indices = {book.select(bytes([value]) * 32, "m-1").line_id for value in range(8)}
    assert len(indices) > 1


def test_selection_rejects_a_wrong_length_beacon() -> None:
    with pytest.raises(OpeningBookError, match="32 bytes"):
        load_book("v1").select(b"\x00" * 31, "m-1")


def test_selection_is_close_to_uniform() -> None:
    """Chi-square smoke test over 10k deterministic draws."""
    book = load_book("v1")
    rng = random.Random(20260101)
    counts = [0] * len(book)
    draws = 10_000
    for _ in range(draws):
        beacon = rng.randbytes(32)
        counts[book.select(beacon, "match").line_id] += 1
    expected = draws / len(book)
    statistic = sum((count - expected) ** 2 / expected for count in counts)
    # df = len(book) - 1; 175 is above the 0.999 quantile for any book up to 130 lines.
    assert statistic < 175, f"chi-square {statistic:.1f} for {len(book)} lines"
    assert min(counts) > 0


def test_line_lookup_rejects_unknown_ids() -> None:
    book = load_book("v1")
    with pytest.raises(OpeningBookError, match="no line"):
        book.line(len(book))
    with pytest.raises(OpeningBookError, match="no line"):
        book.line(-1)


def test_select_on_an_empty_book_is_an_error() -> None:
    empty = OpeningBook(version="empty", lines=())
    with pytest.raises(OpeningBookError, match="empty"):
        empty.select(bytes(32), "m")


def test_unknown_book_and_missing_file(tmp_path: Path) -> None:
    with pytest.raises(OpeningBookError, match="unknown opening book"):
        load_book("v99")
    with pytest.raises(OpeningBookError, match="cannot read"):
        load_books(tmp_path / "missing.json")


def test_invalid_json_is_reported(tmp_path: Path) -> None:
    path = tmp_path / "openings.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(OpeningBookError, match="not valid JSON"):
        load_books(path)


def test_books_are_cached_by_resolved_path() -> None:
    assert load_books() is load_books()


def test_book_must_be_an_object() -> None:
    document = {"format": 1, "books": {"book": ["not", "a", "book"]}}
    with pytest.raises(OpeningBookError, match="not an object"):
        books_from_document(document)


def test_default_book_path_reports_a_missing_book(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("sccp.chess.openings.PACKAGED_BOOK_PATH", tmp_path / "packaged.json")
    monkeypatch.setattr("sccp.chess.openings.REPOSITORY_BOOK_PATH", tmp_path / "repo.json")
    with pytest.raises(OpeningBookError, match="opening book not found"):
        default_book_path()


@pytest.mark.parametrize(
    "document",
    [
        [],
        {"format": 2, "books": {}},
        {"format": 1, "books": {}},
        {"format": 1, "books": {"book": {}}},
        {"format": 1, "books": {"book": {"lines": []}}},
        {"format": 1, "books": {"": {"lines": [{"name": "A", "moves": ["e4"]}]}}},
        {"format": 1, "books": {"book": {"lines": ["not an object"]}}},
    ],
)
def test_invalid_documents_are_rejected(document: object) -> None:
    with pytest.raises(OpeningBookError):
        books_from_document(document)


@pytest.mark.parametrize(
    ("line", "message"),
    [
        ({"moves": ["e4"]}, "name"),
        ({"name": "", "moves": ["e4"]}, "name"),
        ({"name": "A"}, "move list"),
        ({"name": "A", "moves": []}, "move list"),
        ({"name": "A", "moves": ["e4", 5]}, "non-string"),
        ({"name": "A", "moves": ["e4"] * 13}, "cap"),
        ({"name": "A", "moves": ["e4", "e5", "Ke3"]}, "illegal"),
        ({"name": "A", "moves": ["f3", "e5", "g4", "Qh4#", "Nc3"]}, "ended"),
    ],
)
def test_invalid_lines_are_rejected(line: dict[str, object], message: str) -> None:
    with pytest.raises(OpeningBookError, match=message):
        books_from_document(_document([line]))


def test_duplicate_line_names_are_rejected() -> None:
    lines = [
        {"name": "Same", "moves": ["e4", "e5"]},
        {"name": "Same", "moves": ["d4", "d5"]},
    ]
    with pytest.raises(OpeningBookError, match="duplicate"):
        books_from_document(_document(lines))


def test_line_san_is_canonicalised() -> None:
    # A missing check marker is accepted but stored canonically.
    book = books_from_document(
        _document([{"name": "Check", "moves": ["e4", "e5", "Bc4", "Nc6", "Qh5", "Nf6", "Qxf7"]}])
    )["book"]
    assert book.line(0).moves[-1] == "Qxf7#"


def test_document_accepts_multiple_books() -> None:
    document = {
        "format": 1,
        "books": {
            "one": {"lines": [{"name": "A", "moves": ["e4"]}]},
            "two": {"lines": [{"name": "B", "moves": ["d4"]}]},
        },
    }
    books = books_from_document(document)
    assert set(books) == {"one", "two"}
    assert books["two"].line(0).opening_hash != books["one"].line(0).opening_hash


def test_document_from_json_file(tmp_path: Path) -> None:
    path = tmp_path / "openings.json"
    path.write_text(json.dumps(_document([{"name": "A", "moves": ["e4", "e5"]}])), encoding="utf-8")
    book = load_book("book", path)
    assert isinstance(book, OpeningBook)
    assert book.line(0).moves == ("e4", "e5")
    assert book.line(0).plies == 2


def test_opening_line_replay_matches_the_book() -> None:
    line = load_book("v1").line(3)
    _replay(line.moves)
    assert (
        line.opening_hash
        == OpeningLine(
            book_version=line.book_version,
            line_id=line.line_id,
            name=line.name,
            moves=line.moves,
        ).opening_hash
    )
