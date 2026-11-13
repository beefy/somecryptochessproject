"""Unit tests for bech32 addresses."""

from __future__ import annotations

from typing import Any

import pytest

from sccp.config import DEVNET, MAINNET, NETWORKS, TESTNET, NetworkParams
from sccp.crypto.address import (
    ADDRESS_VERSION,
    HASH_SIZE,
    PROGRAM_SIZE,
    address_for_public_key,
    address_hash,
    address_program,
    decode_address,
    encode_address,
    validate_address,
)
from sccp.crypto.pq import hybrid_keypair_from_seed
from sccp.errors import AddressError, KeyEncodingError

Vectors = dict[str, Any]


@pytest.fixture(scope="module")
def public_key(vectors: Vectors) -> bytes:
    keypair = hybrid_keypair_from_seed(bytes.fromhex(vectors["seed_hex"]))
    return keypair.public_key_bytes


@pytest.mark.parametrize("network", list(NETWORKS.values()))
def test_encode_matches_golden(vectors: Vectors, public_key: bytes, network: NetworkParams) -> None:
    assert encode_address(public_key, network) == vectors["addresses"][network.network_id]


def test_addresses_are_lowercase_and_prefixed(public_key: bytes) -> None:
    testnet = encode_address(public_key, TESTNET)
    mainnet = encode_address(public_key, MAINNET)
    assert testnet.startswith("sccpt1") and testnet == testnet.lower()
    assert mainnet.startswith("sccp1") and mainnet == mainnet.lower()
    assert testnet != mainnet


def test_address_program_layout(public_key: bytes) -> None:
    program = address_program(TESTNET.network_id, public_key)
    assert len(program) == PROGRAM_SIZE == 33
    assert program[0] == ADDRESS_VERSION == 1
    assert program[1:] == address_hash("testnet", public_key)
    assert len(program[1:]) == HASH_SIZE == 32


def test_decode_round_trip(vectors: Vectors, public_key: bytes) -> None:
    address = encode_address(public_key, TESTNET)
    decoded = decode_address(address)
    assert decoded.hrp == TESTNET.address_hrp
    assert decoded.version == ADDRESS_VERSION
    assert decoded.payload == address_hash("testnet", public_key)
    assert decoded.program == bytes([ADDRESS_VERSION]) + decoded.payload


def test_uppercase_address_decodes(vectors: Vectors, public_key: bytes) -> None:
    upper = vectors["uppercase_address"]
    assert decode_address(upper).program == decode_address(upper.lower()).program
    assert validate_address(upper, TESTNET)


def test_validate_address_is_network_specific(vectors: Vectors) -> None:
    assert validate_address(vectors["addresses"]["testnet"], TESTNET)
    assert validate_address(vectors["addresses"]["testnet"], DEVNET)
    assert validate_address(vectors["addresses"]["mainnet"], MAINNET)
    assert not validate_address(vectors["addresses"]["testnet"], MAINNET)
    assert not validate_address(vectors["addresses"]["mainnet"], TESTNET)


STRUCTURALLY_INVALID = [
    "bad_checksum",
    "mixed_case",
    "wrong_version",
    "short_program",
    "empty",
    "too_long",
]


@pytest.mark.parametrize("key", STRUCTURALLY_INVALID)
def test_invalid_addresses_raise(vectors: Vectors, key: str) -> None:
    address = vectors["invalid_addresses"][key]
    with pytest.raises(AddressError):
        decode_address(address)


@pytest.mark.parametrize("key", [*STRUCTURALLY_INVALID, "wrong_hrp"])
def test_invalid_addresses_do_not_validate(vectors: Vectors, key: str) -> None:
    assert not validate_address(vectors["invalid_addresses"][key], TESTNET)


@pytest.mark.parametrize("bad", [None, 42, b"sccpt1..."])
def test_non_string_addresses_raise(bad: object) -> None:
    with pytest.raises(AddressError):
        decode_address(bad)  # type: ignore[arg-type]


@pytest.mark.parametrize("length", [0, 31, 33])
def test_address_hash_rejects_wrong_key_length(length: int) -> None:
    with pytest.raises(KeyEncodingError):
        address_hash("testnet", bytes(length))


def test_address_hash_is_network_bound(public_key: bytes) -> None:
    assert address_hash("testnet", public_key) != address_hash("mainnet", public_key)


def test_alias_matches_encode(public_key: bytes) -> None:
    assert address_for_public_key(public_key, "mainnet") == encode_address(public_key, MAINNET)


def test_address_error_is_a_value_error() -> None:
    assert issubclass(AddressError, ValueError)
