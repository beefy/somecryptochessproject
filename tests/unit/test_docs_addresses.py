"""Address literals quoted in the guides must still parse.

The guides copy addresses from real runs, and an address is the one value in a
guide that a reader will paste verbatim: a stale one fails immediately with
"unsupported address version" or a bad checksum, and the reader has no way to
tell whether the guide or their machine is wrong. The one-scheme refactor moved
addresses to version 1 — a program committing to the 1985-byte hybrid public
key — so every literal written before it needed re-deriving.

This scans the prose (and the compose file's sibling docs) for full literals and
parses each one. Placeholders and truncated addresses (`sccpt1...`,
`sccpt1q8f7...`) are deliberately out of scope: the pattern only matches strings
long enough to be a real address.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from sccp.crypto.address import AddressError, decode_address

ROOT = Path(__file__).resolve().parents[2]
PATTERN = re.compile(r"sccp[a-z]?1[0-9a-z]{20,}")


def _documents() -> list[Path]:
    return [
        *sorted((ROOT / "docs").rglob("*.md")),
        ROOT / "README.md",
        *sorted((ROOT / "deploy").glob("*.md")),
    ]


def _literals() -> list[tuple[Path, int, str]]:
    found: list[tuple[Path, int, str]] = []
    for path in _documents():
        text = path.read_text(encoding="utf-8")
        for line_no, line in enumerate(text.splitlines(), 1):
            for literal in PATTERN.findall(line):
                found.append((path, line_no, literal))
    return found


def test_the_guides_quote_at_least_one_address() -> None:
    """A pattern that matches nothing would make the check below vacuous."""
    assert _literals()


@pytest.mark.parametrize(
    ("path", "line_no", "literal"),
    _literals(),
    ids=lambda value: str(value).replace(str(ROOT) + "/", ""),
)
def test_every_quoted_address_parses(path: Path, line_no: int, literal: str) -> None:
    try:
        decode_address(literal)
    except AddressError as exc:
        pytest.fail(f"{path.relative_to(ROOT)}:{line_no} quotes {literal}: {exc}")
