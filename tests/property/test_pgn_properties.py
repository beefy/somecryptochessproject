"""Hypothesis invariants for the hostile-input PGN boundary (plan section 7.2)."""

from __future__ import annotations

import contextlib

from hypothesis import given, settings
from hypothesis import strategies as st

from sccp.chess.pgn import canonical_game_hash, validate_pgn
from sccp.config import TESTNET
from sccp.errors import PgnValidationError

TEXT = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",)),
    max_size=300,
)


@settings(max_examples=100, deadline=None)
@given(TEXT)
def test_hostile_pgn_never_raises_anything_else(text: str) -> None:
    """A parser boundary may reject anything, but only with PgnValidationError."""
    with contextlib.suppress(PgnValidationError):
        validate_pgn(text, params=TESTNET)


@settings(max_examples=100, deadline=None)
@given(st.lists(st.text(max_size=8), max_size=10))
def test_game_hash_is_deterministic_for_any_san_list(moves: list[str]) -> None:
    first = canonical_game_hash("testnet", moves, "1-0")
    assert first == canonical_game_hash("testnet", moves, "1-0")
    assert len(first) == 64
    assert first == first.lower()
