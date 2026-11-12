"""Beacon commitment, derivation and drand checks (PROTOCOL.md section 6)."""

from __future__ import annotations

import json
from hashlib import sha256

import pytest

from sccp.errors import BeaconError
from sccp.protocol.beacon import (
    DRAND_QUICKNET_PERIOD_SECONDS,
    DRAND_QUICKNET_ROUND_OFFSET,
    beacon_digest,
    beacon_hash,
    commitment,
    compute_beacon,
    derive_drand_round,
    epoch_reveals,
    median_time_past,
    reveal_for,
    validate_reveal,
    verify_beacon,
)
from sccp.protocol.models import Availability, BeaconReveal, Reveal

NONCE = "11" * 32
OTHER_NONCE = "22" * 32
BLOCK_HASH = "ab" * 32
DRAND_SIGNATURE = "cd" * 48


def _availability(miner: str, nonce: str = NONCE, *, epoch: int = 3) -> Availability:
    return Availability(
        network="devnet",
        miner=miner,
        epoch=epoch,
        max_games=3,
        commitment=commitment(miner, epoch, nonce),
    )


def _reveal(availability: Availability, nonce: str = NONCE) -> BeaconReveal:
    return BeaconReveal(
        miner=availability.miner,
        commitment=availability.commitment,
        nonce=nonce,
    )


def test_commitment_golden_against_independent_encoding(protocol_accounts) -> None:
    """The commitment is domain + 0x00 + canonical JSON, with no network field."""
    address = protocol_accounts["alice"].address
    payload = {"address": address, "epoch": 3, "nonce": NONCE}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )
    expected = sha256(b"sccp/v1/beacon-commit\x00" + encoded).hexdigest()
    assert commitment(address, 3, NONCE) == expected


def test_commitment_binds_address_epoch_and_nonce(protocol_accounts) -> None:
    alice = protocol_accounts["alice"].address
    bob = protocol_accounts["bob"].address
    base = commitment(alice, 3, NONCE)
    assert commitment(alice, 3, OTHER_NONCE) != base
    assert commitment(alice, 4, NONCE) != base
    assert commitment(bob, 3, NONCE) != base


@pytest.mark.parametrize(
    ("nonce", "epoch"),
    [("zz" * 32, 3), (NONCE, -1), (NONCE, 2**64)],
)
def test_commitment_rejects_bad_inputs(protocol_accounts, nonce: str, epoch: int) -> None:
    with pytest.raises(BeaconError):
        commitment(protocol_accounts["alice"].address, epoch, nonce)


def test_reveal_for_and_validate_reveal(protocol_accounts) -> None:
    availability = _availability(protocol_accounts["alice"].address)
    reveal = reveal_for(availability, NONCE)
    entry = validate_reveal(reveal, availability)
    assert entry.commitment == availability.commitment
    assert entry.nonce == NONCE

    with pytest.raises(BeaconError):
        reveal_for(availability, OTHER_NONCE)

    wrong = reveal.model_copy(update={"miner": protocol_accounts["bob"].address})
    with pytest.raises(BeaconError):
        validate_reveal(wrong, availability)


def test_epoch_reveals_detects_withholding_and_orphans(protocol_accounts) -> None:
    alice = _availability(protocol_accounts["alice"].address, NONCE)
    bob = _availability(protocol_accounts["bob"].address, OTHER_NONCE)
    availability = {alice.miner: alice, bob.miner: bob}

    entries, withheld = epoch_reveals(
        availability,
        [reveal_for(alice, NONCE)],
        network="devnet",
        epoch=3,
    )
    assert [entry.miner for entry in entries] == [alice.miner]
    assert withheld == (bob.miner,)

    carol = _availability(protocol_accounts["carol"].address)
    orphan = Reveal(
        network="devnet",
        miner=carol.miner,
        epoch=3,
        nonce=NONCE,
    )
    with pytest.raises(BeaconError, match="no availability"):
        epoch_reveals(availability, [orphan], network="devnet", epoch=3)

    with pytest.raises(BeaconError, match="expected devnet epoch 4"):
        epoch_reveals(availability, [], network="devnet", epoch=4)

    with pytest.raises(BeaconError, match="does not match miner"):
        epoch_reveals(
            {"sccpt1wrong": alice},
            [],
            network="devnet",
            epoch=3,
        )


def test_beacon_hash_is_order_independent(protocol_accounts) -> None:
    alice = _availability(protocol_accounts["alice"].address, NONCE)
    bob = _availability(protocol_accounts["bob"].address, OTHER_NONCE)
    reveals = [_reveal(alice), _reveal(bob, OTHER_NONCE)]
    forward = beacon_hash("devnet", 3, BLOCK_HASH, reveals)
    backward = beacon_hash("devnet", 3, BLOCK_HASH, list(reversed(reveals)))
    assert forward == backward
    assert forward == beacon_digest("devnet", 3, BLOCK_HASH, reveals).hex()


def test_beacon_hash_binds_every_input(protocol_accounts) -> None:
    alice = _availability(protocol_accounts["alice"].address, NONCE)
    base = beacon_hash("devnet", 3, BLOCK_HASH, [_reveal(alice)])
    assert beacon_hash("devnet", 3, BLOCK_HASH, []) != base
    assert beacon_hash("devnet", 4, BLOCK_HASH, [_reveal(alice)]) != base
    assert beacon_hash("devnet", 3, "cd" * 32, [_reveal(alice)]) != base
    assert beacon_hash("testnet", 3, BLOCK_HASH, [_reveal(alice)]) != base
    assert (
        beacon_hash("devnet", 3, BLOCK_HASH, [_reveal(alice)], drand_signature=DRAND_SIGNATURE)
        != base
    )


def test_compute_beacon_sets_flags_and_sorts_reveals(protocol_accounts) -> None:
    alice = _availability(protocol_accounts["alice"].address, NONCE)
    bob = _availability(protocol_accounts["bob"].address, OTHER_NONCE)
    beacon = compute_beacon(
        network="devnet",
        epoch=3,
        block_hash=BLOCK_HASH,
        reveals=[_reveal(bob, OTHER_NONCE), _reveal(alice)],
    )
    assert beacon.drand_used is False
    assert beacon.drand_round is None
    assert beacon.drand_signature is None
    assert {reveal.miner for reveal in beacon.reveals} == {alice.miner, bob.miner}
    verify_beacon(beacon)

    mixed = compute_beacon(
        network="devnet",
        epoch=3,
        block_hash=BLOCK_HASH,
        reveals=[_reveal(alice)],
        drand_round=7,
        drand_signature=DRAND_SIGNATURE,
    )
    assert mixed.drand_used is True
    assert mixed.drand_round == 7
    verify_beacon(mixed, drand_verifier=lambda round_number, signature: round_number == 7)


def test_verify_beacon_accepts_valid_reveals_and_availability(protocol_accounts) -> None:
    alice = _availability(protocol_accounts["alice"].address, NONCE)
    beacon = compute_beacon(
        network="devnet",
        epoch=3,
        block_hash=BLOCK_HASH,
        reveals=[_reveal(alice)],
    )
    verify_beacon(
        beacon,
        block_hash=BLOCK_HASH,
        availability={alice.miner: alice.commitment},
    )


def test_verify_beacon_rejects_forged_commitment(protocol_accounts) -> None:
    alice = _availability(protocol_accounts["alice"].address, NONCE)
    forged = BeaconReveal(miner=alice.miner, commitment="00" * 32, nonce=NONCE)
    beacon = compute_beacon(
        network="devnet",
        epoch=3,
        block_hash=BLOCK_HASH,
        reveals=[forged],
    )
    with pytest.raises(BeaconError, match="does not open"):
        verify_beacon(beacon)


def test_verify_beacon_rejects_availability_mismatch(protocol_accounts) -> None:
    alice = _availability(protocol_accounts["alice"].address, NONCE)
    beacon = compute_beacon(
        network="devnet",
        epoch=3,
        block_hash=BLOCK_HASH,
        reveals=[_reveal(alice)],
    )
    with pytest.raises(BeaconError, match="does not match its availability"):
        verify_beacon(beacon, availability={alice.miner: "ff" * 32})
    with pytest.raises(BeaconError, match="no availability"):
        verify_beacon(beacon, availability={})


def test_verify_beacon_rejects_tampering(protocol_accounts) -> None:
    alice = _availability(protocol_accounts["alice"].address, NONCE)
    beacon = compute_beacon(
        network="devnet",
        epoch=3,
        block_hash=BLOCK_HASH,
        reveals=[_reveal(alice)],
    )
    with pytest.raises(BeaconError, match="block hash"):
        verify_beacon(beacon, block_hash="cd" * 32)
    tampered = beacon.model_copy(update={"hash": "00" * 32})
    with pytest.raises(BeaconError, match="recomputes"):
        verify_beacon(tampered)
    wrong_network = beacon.model_copy(update={"network": "mainnet"})
    with pytest.raises(BeaconError, match="address"):
        verify_beacon(wrong_network)


def test_verify_beacon_checks_drand_rules(protocol_accounts) -> None:
    alice = _availability(protocol_accounts["alice"].address, NONCE)
    beacon = compute_beacon(
        network="devnet",
        epoch=3,
        block_hash=BLOCK_HASH,
        reveals=[_reveal(alice)],
        drand_round=7,
        drand_signature=DRAND_SIGNATURE,
    )
    with pytest.raises(BeaconError, match="pinned verifier"):
        verify_beacon(beacon)
    with pytest.raises(BeaconError, match="round"):
        verify_beacon(beacon, drand_verifier=lambda r, s: True, expected_round=8)
    with pytest.raises(BeaconError, match="does not verify"):
        verify_beacon(
            beacon,
            drand_verifier=lambda round_number, signature: signature == "ee" * 48,
        )

    no_signature = beacon.model_copy(update={"drand_signature": None})
    with pytest.raises(BeaconError, match="no signature"):
        verify_beacon(no_signature, drand_verifier=lambda r, s: True)

    unflagged = compute_beacon(
        network="devnet",
        epoch=3,
        block_hash=BLOCK_HASH,
        reveals=[_reveal(alice)],
    ).model_copy(update={"drand_round": 7})
    with pytest.raises(BeaconError, match="drand_used is false"):
        verify_beacon(unflagged)


def test_median_time_past_uses_last_eleven_and_is_platform_stable() -> None:
    assert median_time_past([*list(range(20, 31)), 1, 2]) == 25
    values = [100, 3, 7, 11, 5, 9, 1, 13, 15, 17, 19]
    assert median_time_past(values) == 11
    assert median_time_past(values) == median_time_past(list(reversed(values)))
    with pytest.raises(BeaconError):
        median_time_past([])
    with pytest.raises(BeaconError):
        median_time_past([-1])
    with pytest.raises(BeaconError):
        median_time_past([1.5])  # type: ignore[list-item]


def test_derive_drand_round_is_deterministic() -> None:
    assert derive_drand_round(0) == DRAND_QUICKNET_ROUND_OFFSET
    assert derive_drand_round(2) == 1
    assert derive_drand_round(3) == 2
    assert derive_drand_round(10) == 10 // DRAND_QUICKNET_PERIOD_SECONDS + 1
    assert derive_drand_round(10) == derive_drand_round(10)
    assert derive_drand_round(10, period_seconds=5, offset=0) == 2
    with pytest.raises(BeaconError):
        derive_drand_round(10, period_seconds=0)
    with pytest.raises(BeaconError):
        derive_drand_round(-1)
