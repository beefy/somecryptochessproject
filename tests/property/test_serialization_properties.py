"""Hypothesis invariants for canonical JSON and Merkle commitments."""

from __future__ import annotations

import json
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from sccp.errors import UnsupportedTypeError
from sccp.serialization import INT_MAX, INT_MIN, canonical_json, merkle_root

# NFC-stable, collision-free alphabet: distinct keys never normalize together.
KEYS = st.text(alphabet="abcXYZ019_-", min_size=1, max_size=8)
TEXT = st.text(alphabet="aéü 0XYZ", max_size=8)
SCALARS = st.one_of(st.none(), st.booleans(), st.integers(INT_MIN, INT_MAX), TEXT)
JSON_VALUES = st.recursive(
    SCALARS,
    lambda children: st.one_of(
        st.lists(children, max_size=3),
        st.dictionaries(KEYS, children, max_size=3),
    ),
    max_leaves=8,
)


@settings(max_examples=100, deadline=None)
@given(JSON_VALUES)
def test_canonical_json_is_idempotent(value: Any) -> None:
    encoded = canonical_json(value)
    assert canonical_json(json.loads(encoded)) == encoded


@settings(max_examples=100, deadline=None)
@given(JSON_VALUES)
def test_canonical_json_is_sorted_and_compact(value: Any) -> None:
    encoded = canonical_json(value).decode("utf-8")
    expected = json.dumps(
        json.loads(encoded), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    assert encoded == expected


@settings(max_examples=50, deadline=None)
@given(st.integers(INT_MIN, INT_MAX))
def test_integers_use_plain_decimal(value: int) -> None:
    assert canonical_json(value) == str(value).encode()


@settings(max_examples=50, deadline=None)
@given(st.floats())
def test_floats_are_never_serialized(value: float) -> None:
    with pytest.raises(UnsupportedTypeError):
        canonical_json(value)


@settings(max_examples=50, deadline=None)
@given(st.dictionaries(KEYS, st.one_of(st.none(), st.integers(INT_MIN, INT_MAX)), max_size=5))
def test_merkle_root_is_independent_of_insertion_order(leaves: dict[str, Any]) -> None:
    items = list(leaves.items())
    assert merkle_root(leaves) == merkle_root(dict(reversed(items)))
    assert merkle_root(items) == merkle_root(list(reversed(items)))


@settings(max_examples=50, deadline=None)
@given(st.dictionaries(KEYS, st.integers(INT_MIN, INT_MAX), min_size=1, max_size=5))
def test_merkle_root_ignores_none_values(leaves: dict[str, int]) -> None:
    assert merkle_root({**leaves, "absent": None}) == merkle_root(leaves)
