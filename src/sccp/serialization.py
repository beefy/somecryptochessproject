"""Canonical JSON, domain-separated hashing and Merkle commitments.

This module is the single source of truth for every byte that consensus hashes
(docs/IMPLEMENTATION_PLAN.md section 5.2):

``hash = sha256(domain.encode() + b"\\x00" + canonical_json(payload))``

Floats are rejected by an explicit walk rather than by ``json.dumps``, because
``json.dumps(allow_nan=False)`` still accepts finite floats such as ``1.5``.
The walk also enforces the consensus integer range, NFC-normalizes keys and
values, and refuses keys that collide after normalization.
"""

from __future__ import annotations

import json
import unicodedata
from collections.abc import Iterable, Mapping
from enum import StrEnum
from hashlib import sha256
from typing import Final, TypeAlias

from sccp.errors import (
    DuplicateKeyError,
    RangeError,
    SerializationError,
    UnsupportedTypeError,
)

JsonScalar: TypeAlias = bool | int | str | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
"""The complete set of values allowed inside a signed or hashed payload."""

INT_MIN: Final = -(2**63)
INT_MAX: Final = 2**64 - 1

DIGEST_SIZE: Final = 32
EMPTY_ROOT: Final[bytes] = b"\x00" * DIGEST_SIZE
"""Root of an empty commitment tree."""


def normalize(value: object) -> JsonValue:
    """Type-check, range-check and NFC-normalize a canonical JSON tree.

    Accepts ``None``, ``bool``, ``int``, ``str``, ``list`` and ``dict`` (including
    subclasses of the latter two). Everything else -- notably ``float``,
    ``Decimal``, ``bytes``, ``tuple`` and ``set`` -- raises
    :class:`~sccp.errors.UnsupportedTypeError`.
    """
    if value is None or isinstance(value, bool):  # bool before int
        return value
    if isinstance(value, int):
        if not INT_MIN <= value <= INT_MAX:
            raise RangeError(f"integer {value} outside [{INT_MIN}, {INT_MAX}]")
        return value
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, list):
        return [normalize(item) for item in value]
    if isinstance(value, dict):
        items: dict[object, object] = value
        out: dict[str, JsonValue] = {}
        for raw_key, item in items.items():
            if not isinstance(raw_key, str):
                raise UnsupportedTypeError("object keys must be strings")
            key = unicodedata.normalize("NFC", raw_key)
            if key in out:
                raise DuplicateKeyError(f"duplicate object key after NFC: {key!r}")
            out[key] = normalize(item)
        return out
    raise UnsupportedTypeError(f"unsupported canonical JSON type: {type(value).__name__}")


def canonical_json(value: object) -> bytes:
    """Encode ``value`` as canonical UTF-8 JSON (sorted keys, no whitespace)."""
    encoded = json.dumps(
        normalize(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,  # belt and braces; no float survives normalize()
    )
    return encoded.encode("utf-8")


class Domain(StrEnum):
    """Hash domains from docs/IMPLEMENTATION_PLAN.md section 5.2."""

    ADDRESS = "sccp/v1/address"
    BEACON = "sccp/v1/beacon"
    BEACON_COMMIT = "sccp/v1/beacon-commit"
    BLOCK = "sccp/v1/block"
    CERTIFICATE = "sccp/v1/certificate"
    CHALLENGE = "sccp/v1/challenge"
    CLOCK = "sccp/v1/clock"
    GAME = "sccp/v1/game"
    HEADER = "sccp/v1/header"
    MATCH = "sccp/v1/match"
    MOVE = "sccp/v1/move"
    OPENING = "sccp/v1/opening"
    PAIR = "sccp/v1/pair"
    PROOF = "sccp/v1/proof"
    RESULT = "sccp/v1/result"
    TX = "sccp/v1/tx"


DOMAINS: Final[frozenset[str]] = frozenset(domain.value for domain in Domain)


def check_domain(domain: str) -> str:
    """Return ``domain`` if it is a registered domain, else raise."""
    if domain not in DOMAINS:
        raise SerializationError(f"unknown hash domain: {domain!r}")
    return domain


def domain_hash(domain: str, payload: object) -> bytes:
    """``sha256(domain + 0x00 + canonical_json(payload))`` as raw bytes."""
    prefix = check_domain(domain).encode("utf-8") + b"\x00"
    return sha256(prefix + canonical_json(payload)).digest()


def domain_hex(domain: str, payload: object) -> str:
    """Lowercase hex form of :func:`domain_hash`."""
    return domain_hash(domain, payload).hex()


def merkle_leaf(key: str, value: object) -> bytes:
    """Leaf hash: ``sha256(0x00 + utf8(key) + 0x00 + canonical_json(value))``."""
    return sha256(b"\x00" + key.encode("utf-8") + b"\x00" + canonical_json(value)).digest()


def merkle_node(left: bytes, right: bytes) -> bytes:
    """Internal node hash: ``sha256(0x01 + left + right)``."""
    if len(left) != DIGEST_SIZE or len(right) != DIGEST_SIZE:
        raise SerializationError("merkle nodes must be 32-byte digests")
    return sha256(b"\x01" + left + right).digest()


def merkle_root(leaves: Mapping[str, object] | Iterable[tuple[str, object]]) -> bytes:
    """Commit to a ``key -> value`` map, independent of insertion order.

    Leaves are sorted by the UTF-8 bytes of their keys. ``value is None`` means
    "absent" and is not encoded, matching docs/IMPLEMENTATION_PLAN.md section 5.2.
    An odd node at any level is promoted unchanged. An empty tree is
    :data:`EMPTY_ROOT`.
    """
    if isinstance(leaves, Mapping):
        pairs: list[tuple[str, object]] = list(leaves.items())
    else:
        pairs = list(leaves)

    normalized: list[tuple[str, object]] = []
    for key, value in pairs:
        if not isinstance(key, str):
            raise UnsupportedTypeError("merkle keys must be strings")
        normalized.append((unicodedata.normalize("NFC", key), value))

    normalized.sort(key=lambda item: item[0].encode("utf-8"))

    level: list[bytes] = []
    previous_key: str | None = None
    for key, value in normalized:
        if key == previous_key:
            raise DuplicateKeyError(f"duplicate merkle key after NFC: {key!r}")
        previous_key = key
        if value is None:
            continue
        level.append(merkle_leaf(key, value))

    if not level:
        return EMPTY_ROOT
    while len(level) > 1:
        level = [
            merkle_node(level[i], level[i + 1]) if i + 1 < len(level) else level[i]
            for i in range(0, len(level), 2)
        ]
    return level[0]


def merkle_root_hex(leaves: Mapping[str, object] | Iterable[tuple[str, object]]) -> str:
    """Lowercase hex form of :func:`merkle_root`."""
    return merkle_root(leaves).hex()
