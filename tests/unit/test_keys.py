"""Unit tests for Ed25519 keys, signatures and domain-separated signing."""

from __future__ import annotations

from typing import Any

import pytest

from sccp.crypto.keys import (
    PUBLIC_KEY_SIZE,
    SEED_SIZE,
    SIGNATURE_SIZE,
    generate_keypair,
    keypair_from_seed,
    public_key_from_bytes,
    public_key_from_hex,
    sign,
    sign_domain,
    verify,
    verify_domain,
)
from sccp.errors import KeyEncodingError

Vectors = dict[str, Any]
MESSAGE = b"sccp test message"


def test_generate_keypair_has_canonical_sizes() -> None:
    keypair = generate_keypair()
    assert len(keypair.seed) == SEED_SIZE
    assert len(keypair.public_key_bytes) == PUBLIC_KEY_SIZE
    assert len(keypair.public_key_hex) == 2 * PUBLIC_KEY_SIZE


def test_keypair_from_seed_matches_golden(vectors: Vectors) -> None:
    keypair = keypair_from_seed(bytes.fromhex(vectors["seed_hex"]))
    assert keypair.public_key_hex == vectors["public_key_hex"]
    assert keypair.seed.hex() == vectors["seed_hex"]


@pytest.mark.parametrize("length", [0, 1, 31, 33, 64])
def test_keypair_from_seed_rejects_wrong_length(length: int) -> None:
    with pytest.raises(KeyEncodingError):
        keypair_from_seed(bytes(length))


def test_sign_verify_round_trip() -> None:
    keypair = generate_keypair()
    signature = sign(keypair, MESSAGE)
    assert len(signature) == SIGNATURE_SIZE
    assert verify(keypair.public_key, MESSAGE, signature)
    assert verify(keypair.public_key_bytes, MESSAGE, signature)


def test_sign_accepts_private_key_or_keypair() -> None:
    keypair = generate_keypair()
    assert sign(keypair.private_key, MESSAGE) == sign(keypair, MESSAGE)


def test_verify_rejects_tampering() -> None:
    keypair = generate_keypair()
    other = generate_keypair()
    signature = sign(keypair, MESSAGE)
    assert not verify(keypair.public_key, MESSAGE + b"!", signature)
    assert not verify(other.public_key, MESSAGE, signature)
    flipped = bytes([signature[0] ^ 0x01]) + signature[1:]
    assert not verify(keypair.public_key, MESSAGE, flipped)


@pytest.mark.parametrize("length", [0, 1, 63, 65, 128])
def test_verify_rejects_wrong_signature_length(length: int) -> None:
    keypair = generate_keypair()
    assert not verify(keypair.public_key, MESSAGE, bytes(length))


@pytest.mark.parametrize("length", [0, 31, 33, 64])
def test_verify_rejects_wrong_key_length(length: int) -> None:
    assert not verify(bytes(length), MESSAGE, bytes(SIGNATURE_SIZE))


def test_verify_is_total_for_arbitrary_bytes() -> None:
    for byte in range(8):
        assert not verify(bytes([byte]) * PUBLIC_KEY_SIZE, MESSAGE, bytes([byte]) * SIGNATURE_SIZE)


def test_domain_signature_matches_golden(vectors: Vectors) -> None:
    keypair = keypair_from_seed(bytes.fromhex(vectors["seed_hex"]))
    golden = vectors["signature"]
    signature = sign_domain(keypair, golden["domain"], golden["payload"])
    assert signature.hex() == golden["hex"]
    assert verify_domain(keypair.public_key, golden["domain"], golden["payload"], signature)


def test_domain_signatures_are_bound_to_domain_and_payload() -> None:
    keypair = generate_keypair()
    payload = {"game": "abc", "ply": 1}
    signature = sign_domain(keypair, "sccp/v1/game", payload)
    assert not verify_domain(keypair.public_key, "sccp/v1/header", payload, signature)
    tampered = {"game": "abc", "ply": 2}
    assert not verify_domain(keypair.public_key, "sccp/v1/game", tampered, signature)


def test_public_key_parsing() -> None:
    keypair = generate_keypair()
    parsed = public_key_from_bytes(keypair.public_key_bytes)
    assert parsed.public_bytes_raw() == keypair.public_key_bytes
    assert public_key_from_hex(keypair.public_key_hex).public_bytes_raw() == (
        keypair.public_key_bytes
    )


@pytest.mark.parametrize("bad", ["zz" * 32, "00" * 31, "00" * 33, ""])
def test_public_key_hex_errors(bad: str) -> None:
    with pytest.raises(KeyEncodingError):
        public_key_from_hex(bad)


def test_key_encoding_error_is_a_value_error() -> None:
    assert issubclass(KeyEncodingError, ValueError)
