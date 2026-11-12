"""Unit tests for canonical JSON, hash domains and Merkle commitments."""

from __future__ import annotations

from decimal import Decimal
from hashlib import sha256
from typing import Any

import pytest

from sccp.errors import (
    DuplicateKeyError,
    RangeError,
    SerializationError,
    UnsupportedTypeError,
)
from sccp.serialization import (
    DIGEST_SIZE,
    EMPTY_ROOT,
    INT_MAX,
    INT_MIN,
    Domain,
    canonical_json,
    domain_hash,
    merkle_leaf,
    merkle_node,
    merkle_root,
    merkle_root_hex,
    normalize,
)

Vectors = dict[str, Any]


def test_canonical_json_matches_golden(vectors: Vectors) -> None:
    golden = vectors["canonical_json"]
    encoded = canonical_json(golden["input"])
    assert encoded.hex() == golden["utf8_hex"]
    assert sha256(encoded).hexdigest() == golden["sha256_hex"]


def test_canonical_json_sorts_keys_and_drops_whitespace() -> None:
    assert canonical_json({"b": [True, None, "x"], "a": 1}) == b'{"a":1,"b":[true,null,"x"]}'


def test_canonical_json_keeps_utf8_unescaped() -> None:
    assert canonical_json({"a": "é"}) == '{"a":"é"}'.encode()


def test_canonical_json_normalizes_to_nfc() -> None:
    composed = {"name": "José"}
    decomposed = {"name": "Jose\u0301"}
    assert canonical_json(composed) == canonical_json(decomposed)
    assert normalize(decomposed) == {"name": "José"}


def test_canonical_json_distinguishes_bool_from_int() -> None:
    assert canonical_json({"t": True, "f": False, "one": 1}) == b'{"f":false,"one":1,"t":true}'


@pytest.mark.parametrize(
    "value",
    [1.5, 0.0, float("inf"), Decimal("1"), b"bytes", (1, 2), {1, 2}, object()],
)
def test_unsupported_types_raise(value: object) -> None:
    with pytest.raises(UnsupportedTypeError):
        canonical_json({"value": value})


def test_unsupported_type_is_a_type_error_and_serialization_error() -> None:
    with pytest.raises(TypeError):
        canonical_json({"value": 1.5})
    with pytest.raises(SerializationError):
        canonical_json({"value": 1.5})


def test_integer_bounds_are_inclusive() -> None:
    assert normalize(INT_MIN) == INT_MIN
    assert normalize(INT_MAX) == INT_MAX
    assert canonical_json([INT_MIN, INT_MAX]) == f"[{INT_MIN},{INT_MAX}]".encode()


@pytest.mark.parametrize("value", [INT_MIN - 1, INT_MAX + 1, 2**64, -(2**63) - 1])
def test_integer_out_of_range_raises_range_error(value: int) -> None:
    with pytest.raises(RangeError):
        canonical_json(value)
    assert issubclass(RangeError, ValueError)


def test_non_string_keys_are_rejected() -> None:
    with pytest.raises(UnsupportedTypeError):
        canonical_json({1: "x"})


def test_keys_colliding_after_nfc_are_rejected() -> None:
    with pytest.raises(DuplicateKeyError):
        canonical_json({"é": 1, "e\u0301": 2})


def test_domain_hash_matches_golden(vectors: Vectors) -> None:
    golden = vectors["domain_hash"]
    assert domain_hash(golden["domain"], golden["payload"]).hex() == golden["hex"]


def test_domain_hash_is_domain_separated(vectors: Vectors) -> None:
    payload = {"a": 1}
    digests = {domain_hash(domain, payload) for domain in Domain}
    assert len(digests) == len(list(Domain))


def test_domains_share_the_protocol_prefix() -> None:
    assert all(domain.value.startswith("sccp/v1/") for domain in Domain)


@pytest.mark.parametrize("domain", ["", "sccp/v2/game", "game", "sccp/v1/unknown"])
def test_unknown_domain_raises(domain: str) -> None:
    with pytest.raises(SerializationError):
        domain_hash(domain, {})


def test_merkle_root_matches_golden(vectors: Vectors) -> None:
    golden = vectors["merkle"]
    assert merkle_root_hex(golden["leaves"]) == golden["root_hex"]


def test_merkle_root_is_independent_of_insertion_order(vectors: Vectors) -> None:
    leaves = vectors["merkle"]["leaves"]
    reordered = dict(reversed(list(leaves.items())))
    assert merkle_root(leaves) == merkle_root(reordered)
    assert merkle_root(leaves) == merkle_root(list(reordered.items()))


def test_merkle_root_skips_none_values() -> None:
    assert merkle_root({"a": 1, "b": None}) == merkle_root({"a": 1})
    assert merkle_root({"a": None}) == EMPTY_ROOT
    assert merkle_root({}) == EMPTY_ROOT


def test_merkle_root_promotes_odd_nodes() -> None:
    leaves = {"a": 1, "b": 2, "c": 3}
    first_pair = merkle_node(merkle_leaf("a", 1), merkle_leaf("b", 2))
    expected = merkle_node(first_pair, merkle_leaf("c", 3))
    assert merkle_root(leaves) == expected


def test_merkle_root_changes_with_any_value() -> None:
    base = {"a": 1, "b": 2, "c": 3}
    assert merkle_root(base) != merkle_root({**base, "c": 4})
    assert merkle_root(base) != merkle_root({"a": 1, "b": 2})


def test_merkle_leaf_and_node_layout() -> None:
    assert merkle_leaf("k", 1) == sha256(b"\x00k\x001").digest()
    left = merkle_leaf("a", 1)
    right = merkle_leaf("b", 2)
    assert merkle_node(left, right) == sha256(b"\x01" + left + right).digest()
    assert len(merkle_node(left, right)) == DIGEST_SIZE


@pytest.mark.parametrize("bad", [b"", b"short", b"x" * 31, b"x" * 33])
def test_merkle_node_rejects_wrong_lengths(bad: bytes) -> None:
    with pytest.raises(SerializationError):
        merkle_node(bad, b"\x00" * DIGEST_SIZE)


def test_merkle_root_rejects_duplicate_keys() -> None:
    with pytest.raises(DuplicateKeyError):
        merkle_root([("a", 1), ("a", 2)])


def test_merkle_root_rejects_duplicates_after_nfc() -> None:
    with pytest.raises(DuplicateKeyError):
        merkle_root({"é": 1, "e\u0301": 2})
