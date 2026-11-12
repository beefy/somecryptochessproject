"""Hypothesis invariants for addresses, keys and mnemonic backup."""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from sccp.config import NETWORKS
from sccp.crypto.address import address_hash, decode_address, encode_address, validate_address
from sccp.crypto.keys import SEED_SIZE, keypair_from_seed, verify
from sccp.crypto.wallet import mnemonic_to_seed, seed_to_mnemonic
from sccp.errors import KeyEncodingError

SEEDS = st.binary(min_size=SEED_SIZE, max_size=SEED_SIZE)


@settings(max_examples=25, deadline=None)
@given(SEEDS)
def test_address_round_trips_on_every_network(seed: bytes) -> None:
    public_key = keypair_from_seed(seed).public_key_bytes
    for params in NETWORKS.values():
        encoded = encode_address(public_key, params)
        decoded = decode_address(encoded)
        assert decoded.payload == address_hash(params.network_id, public_key)
        assert validate_address(encoded, params)


@settings(max_examples=25, deadline=None)
@given(SEEDS)
def test_signatures_verify_for_arbitrary_keys(seed: bytes) -> None:
    keypair = keypair_from_seed(seed)
    message = b"hostile input \x00\xff"
    assert verify(keypair.public_key, message, keypair.private_key.sign(message))
    assert not verify(keypair.public_key, message + b"x", keypair.private_key.sign(message))


@settings(max_examples=25, deadline=None)
@given(SEEDS)
def test_mnemonic_backup_is_lossless(seed: bytes) -> None:
    assert mnemonic_to_seed(seed_to_mnemonic(seed)) == seed


@settings(max_examples=25, deadline=None)
@given(st.binary(min_size=0, max_size=SEED_SIZE - 1))
def test_short_seeds_are_rejected(seed: bytes) -> None:
    with pytest.raises(KeyEncodingError):
        seed_to_mnemonic(seed)
