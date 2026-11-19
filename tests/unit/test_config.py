"""Unit tests for network parameters."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from sccp.config import (
    DEVNET,
    MAINNET,
    NETWORKS,
    TESTNET,
    UNITS_PER_COIN,
    NetworkParams,
    coins_to_units,
    get_network,
    network_ids,
)
from sccp.errors import ConfigError, NetworkError


def test_registry_contains_all_networks() -> None:
    assert network_ids() == ("devnet", "mainnet", "testnet")
    assert set(NETWORKS) == {"devnet", "mainnet", "testnet"}
    assert get_network("mainnet") is MAINNET
    assert get_network(TESTNET) is TESTNET


@pytest.mark.parametrize(
    ("network", "hrp"),
    [(MAINNET, "sccp"), (TESTNET, "sccpt"), (DEVNET, "sccpt")],
)
def test_address_hrps(network: NetworkParams, hrp: str) -> None:
    assert network.address_hrp == hrp


def test_amounts_are_integer_base_units() -> None:
    assert UNITS_PER_COIN == 100_000_000
    assert TESTNET.subsidy == 50 * UNITS_PER_COIN
    assert MAINNET.min_bond == 500 * UNITS_PER_COIN
    assert MAINNET.max_money == 21_000_000 * UNITS_PER_COIN
    assert TESTNET.max_money is None


def test_only_mainnet_enforces_the_opponent_cooldown() -> None:
    """Test networks let a small roster rematch; mainnet keeps the cooldown."""
    assert TESTNET.opponent_cooldown == 0
    assert DEVNET.opponent_cooldown == 0
    assert MAINNET.opponent_cooldown == 4


def test_coins_to_units() -> None:
    assert coins_to_units(0) == 0
    assert coins_to_units(21_000_000) == 2_100_000_000_000_000


@pytest.mark.parametrize("bad", [True, 1.0, "1"])
def test_coins_to_units_rejects_non_integers(bad: object) -> None:
    with pytest.raises(ConfigError):
        coins_to_units(bad)  # type: ignore[arg-type]


def test_unknown_network_raises() -> None:
    with pytest.raises(NetworkError):
        get_network("regtest")
    assert issubclass(NetworkError, ValueError)


def test_clock_and_timeout_parameters() -> None:
    assert TESTNET.lag_grace_ms == 250
    assert TESTNET.timeout_dispute_window == 4
    assert TESTNET.min_timeout_bond == 1 * UNITS_PER_COIN
    assert TESTNET.clock_witness_quorum == 1
    assert TESTNET.clock_witness_bond == 1 * UNITS_PER_COIN
    assert MAINNET.clock_witness_quorum == 3
    assert MAINNET.min_timeout_bond == 10 * UNITS_PER_COIN
    assert MAINNET.clock_witness_bond == 50 * UNITS_PER_COIN


def test_devnet_is_deterministic() -> None:
    assert DEVNET.network_id == "devnet"
    assert DEVNET.epoch_length == 1
    assert DEVNET.retarget_enabled is False
    assert DEVNET.wins_required == 1
    assert DEVNET.min_bond == 0
    assert DEVNET.genesis_timestamp == 1_700_000_000


def test_testnet_windows_are_scaled_to_its_block_time() -> None:
    """Halving the block interval must not halve a proof's or a tx's lifetime."""
    assert TESTNET.target_block_seconds == 30
    assert TESTNET.epoch_length == 5
    # These were 64, 64, 100, 20, 50, 2 and 2 on the old 60-second schedule.
    assert TESTNET.claim_window * TESTNET.target_block_seconds == 64 * 60
    assert TESTNET.mempool_ttl_blocks * TESTNET.target_block_seconds == 64 * 60
    assert TESTNET.snapshot_interval * TESTNET.target_block_seconds == 100 * 60
    assert TESTNET.coinbase_maturity * TESTNET.target_block_seconds == 20 * 60
    assert TESTNET.unbond_delay * TESTNET.target_block_seconds == 50 * 60
    assert TESTNET.timeout_dispute_window * TESTNET.target_block_seconds == 2 * 60
    assert TESTNET.beacon_grace_blocks * TESTNET.target_block_seconds == 2 * 60
    assert TESTNET.registration_epochs == 20  # ten 5-minute epochs


def test_params_are_frozen_and_hashable() -> None:
    assert hash(MAINNET) == hash(get_network("mainnet"))
    with pytest.raises(ValidationError):
        MAINNET.subsidy = 1  # type: ignore[misc]


def test_params_reject_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        NetworkParams(**{**TESTNET.model_dump(), "surprise": True})


def test_params_round_trip_through_dict() -> None:
    assert NetworkParams.model_validate(MAINNET.model_dump()) == MAINNET


INVALID_OVERRIDES: list[dict[str, Any]] = [
    {"retarget_interval": 7},  # not a multiple of epoch_length
    {"wins_required": 0},  # below k_min
    {"wins_required": 6},  # above k_max
    {"availability_lead": 1},  # commit and reveal windows would collide
    {"seal_bits": 33},
    {"min_plies": 301},  # above max_plies
    {"max_plies": 1001},  # above the protocol ceiling
    {"max_block_bytes": 262_144},  # not larger than the PGN cap
    {"max_supply": 21_000_000},  # supply cap without a halving interval
    {"halving_interval": 210_000},  # halving without a supply cap
    {"drand_period_seconds": 3},  # drand timing without a chain
    {"drand_chain": "quicknet", "drand_period_seconds": 3},  # missing round offset
    {"min_fee": 0},
    {"lag_grace_ms": 1_001},
    {"timeout_dispute_window": 0},
    {"clock_witness_quorum": 0},
    {"min_timeout_bond": -1},
]


@pytest.mark.parametrize("override", INVALID_OVERRIDES)
def test_invalid_overrides_are_rejected(override: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        NetworkParams(**{**TESTNET.model_dump(), **override})


def test_amounts_above_max_money_are_rejected() -> None:
    max_money = MAINNET.max_money
    assert max_money is not None
    with pytest.raises(ValidationError):
        NetworkParams(**{**MAINNET.model_dump(), "subsidy": max_money + 1})
