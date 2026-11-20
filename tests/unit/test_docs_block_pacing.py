"""Prose that quotes the chain's block pacing must match `config.py`.

The target block time is consensus, but every sentence built on it is copied by
hand. `TESTNET.target_block_seconds` halved to 30 and the compose settler, every
`--mine-interval-seconds` and every window denominated in blocks followed, while
the prose stayed on the old number: "one block a minute" in the web README, a
bootstrap window "19 minutes" deep in two deploy docstrings and a test, "three
minutes on devnet" where the window is two seconds, and a mirror page that had a
replica fetching "thousands of blocks" for an hour of chain that now holds 120.

Nothing re-derives prose, so this file reads the two numbers an operator acts on
-- how much of a bootstrap window the stack dates ahead of the clock, and how far
ahead that window reaches -- out of the deploy modules, and checks the documents
still spell them the way the code computes them. It also pins the phrasings the
30-second change retired, so a copied paragraph cannot bring one back.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from deploy import bootstrap_devnet_stack, bootstrap_testnet_stack

from sccp.config import DEVNET, TESTNET

ROOT = Path(__file__).resolve().parents[2]

_WORDS: Final = (
    "zero",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
    "thirteen",
    "fourteen",
    "fifteen",
    "sixteen",
    "seventeen",
    "eighteen",
    "nineteen",
    "twenty",
)

RETIRED: Final = (
    # Phrasings that assumed a 60-second target.
    "block a minute",
    "one a minute",
    "one minute each",
    "60-second block target",
    "60 s block",
    # Arithmetic derived from it: an 18-block window is 18 minutes at 60 s and
    # nine at 30, and the devnet window was never minutes at all.
    "19 minutes on testnet",
    "three minutes on devnet",
    # The retired *clock*, which `FIXED_TIME_CONTROL` now sets to 10+0.
    "30+0",
)


def _documents() -> list[Path]:
    """Every file whose prose an operator reads for this number."""
    return [
        *sorted((ROOT / "docs").rglob("*.md")),
        *sorted((ROOT / "deploy").rglob("*.md")),
        *sorted((ROOT / "deploy").glob("*.py")),
        ROOT / "README.md",
        ROOT / "src" / "web" / "README.md",
        ROOT / "docker-compose.yml",
    ]


def _prose() -> list[tuple[Path, str]]:
    """The documents with their whitespace flattened, so wrapping cannot hide a phrase."""
    return [(path, " ".join(path.read_text(encoding="utf-8").split())) for path in _documents()]


def _spelled(seconds: int) -> str:
    """How the documents spell a window: in minutes when it divides evenly."""
    if seconds % 60 == 0:
        return f"{_WORDS[seconds // 60]} minutes"
    return f"{_WORDS[seconds]} seconds"


def test_the_corpus_is_scanned() -> None:
    """An empty corpus would make every check below vacuous."""
    assert len(_documents()) > 20


def test_no_document_still_quotes_the_retired_block_time() -> None:
    hits = [
        f"{path.relative_to(ROOT)} {phrase!r}"
        for path, text in _prose()
        for phrase in RETIRED
        if phrase in text
    ]
    assert not hits, "prose is still on the 60-second block target: " + "; ".join(hits)


def test_the_bootstrap_window_in_the_prose_matches_the_target() -> None:
    """The settler's first real block waits on this window, so the prose is load-bearing."""
    testnet_window = (bootstrap_testnet_stack.BOOTSTRAP_HEIGHT - 1) * TESTNET.target_block_seconds
    devnet_window = (bootstrap_devnet_stack.BOOTSTRAP_HEIGHT - 1) * DEVNET.target_block_seconds
    text = " ".join(text for _, text in _prose())

    assert f"{_spelled(testnet_window)} on testnet" in text
    assert f"{_spelled(devnet_window)} on devnet" in text
