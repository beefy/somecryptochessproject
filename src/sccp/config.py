"""Immutable network parameters (docs/IMPLEMENTATION_PLAN.md section 5.1).

Amounts are integer base units: ``1 coin = 100_000_000 units``. No floating
point value is ever accepted by this module. ``NetworkParams`` validates its own
invariants at construction, so an impossible network definition fails at import
time rather than at block validation time.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from sccp.errors import ConfigError, NetworkError

UNITS_PER_COIN: Final = 100_000_000
"""Base units per coin. All consensus amounts are integers of this unit."""

MAX_PLIES_LIMIT: Final = 1000
"""Hard upper bound for ``max_plies``; longer games are never consensus-valid."""


def coins_to_units(coins: int) -> int:
    """Convert whole coins to base units."""
    if not isinstance(coins, int) or isinstance(coins, bool):
        raise ConfigError("coin amounts must be integers")
    return coins * UNITS_PER_COIN


class QualityMode(StrEnum):
    """Whether the quality policy is enforced for a network."""

    OFF = "off"
    SAMPLED = "sampled"


class NetworkParams(BaseModel):
    """One network's consensus constants. Frozen: instances are hashable and safe."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Identity
    network_id: str = Field(min_length=1)
    address_hrp: str = Field(min_length=1)
    genesis_timestamp: int | None = None

    # Epochs and matchmaking
    epoch_length: int = Field(ge=1)
    rounds_per_epoch: int = Field(ge=1)
    availability_lead: int = Field(ge=2)
    wins_required: int = Field(ge=1)
    k_min: int = Field(ge=1)
    k_max: int = Field(ge=1)
    claim_window: int = Field(ge=1)
    opponent_cooldown: int = Field(ge=0)

    # Header seal and retargeting
    seal_bits: int = Field(ge=0, le=32)
    target_block_seconds: int = Field(ge=1)
    retarget_interval: int = Field(ge=1)
    retarget_enabled: bool = True

    # Economics
    subsidy: int = Field(gt=0)
    halving_interval: int | None = Field(default=None, ge=1)
    max_supply: int | None = Field(default=None, ge=1)
    coinbase_maturity: int = Field(ge=0)
    min_bond: int = Field(ge=0)
    registration_epochs: int = Field(ge=1)
    unbond_delay: int = Field(ge=0)
    min_fee: int = Field(gt=0)
    challenge_window: int = Field(ge=0)

    # Clocks and timeout settlement
    lag_grace_ms: int = Field(default=250, ge=0, le=1_000)
    timeout_dispute_window: int = Field(default=2, ge=1)
    min_timeout_bond: int = Field(default=0, ge=0)
    clock_witness_quorum: int = Field(default=1, ge=1)
    clock_witness_bond: int = Field(default=0, ge=0)

    # Chess rules
    min_plies: int = Field(ge=1)
    max_plies: int = Field(ge=1, le=MAX_PLIES_LIMIT)
    nodes_per_move: int = Field(gt=0)
    book_version: str = Field(min_length=1)

    # Quality policy
    quality_mode: QualityMode
    quality_sample_rate: int = Field(ge=0, le=1000)

    # Beacon
    beacon_grace_blocks: int = Field(ge=0)
    drand_chain: str | None = None
    drand_period_seconds: int | None = Field(default=None, ge=1)
    drand_round_offset: int | None = None

    # Size caps
    max_block_bytes: int = Field(gt=0)
    max_pgn_bytes: int = Field(gt=0)
    max_tx_bytes: int = Field(gt=0)

    @property
    def max_money(self) -> int | None:
        """Supply cap in base units, or ``None`` when the network is uncapped."""
        if self.max_supply is None:
            return None
        return self.max_supply * UNITS_PER_COIN

    @model_validator(mode="after")
    def _check_invariants(self) -> Self:
        if self.retarget_interval % self.epoch_length != 0:
            raise ValueError("retarget_interval must be a multiple of epoch_length")
        if not self.k_min <= self.wins_required <= self.k_max:
            raise ValueError("wins_required must satisfy k_min <= k <= k_max")
        if self.min_plies > self.max_plies:
            raise ValueError("min_plies must not exceed max_plies")
        if self.max_block_bytes <= max(self.max_pgn_bytes, self.max_tx_bytes):
            raise ValueError("max_block_bytes must exceed the PGN and tx caps")
        if (self.max_supply is None) != (self.halving_interval is None):
            raise ValueError("max_supply and halving_interval are set together or not at all")
        drand_fields = (self.drand_period_seconds, self.drand_round_offset)
        if self.drand_chain is None and any(field is not None for field in drand_fields):
            raise ValueError("drand timing constants require drand_chain")
        if self.drand_chain is not None and any(field is None for field in drand_fields):
            raise ValueError("drand_chain requires drand_period_seconds and drand_round_offset")
        if self.quality_mode is QualityMode.SAMPLED and self.quality_sample_rate == 0:
            raise ValueError("quality_mode 'sampled' requires a non-zero quality_sample_rate")

        max_money = self.max_money
        if max_money is not None:
            for name, amount in (
                ("subsidy", self.subsidy),
                ("min_bond", self.min_bond),
                ("min_fee", self.min_fee),
                ("min_timeout_bond", self.min_timeout_bond),
                ("clock_witness_bond", self.clock_witness_bond),
            ):
                if amount > max_money:
                    raise ValueError(f"{name} exceeds MAX_MONEY ({max_money})")
        return self


TESTNET: Final = NetworkParams(
    network_id="testnet",
    address_hrp="sccpt",
    epoch_length=5,
    rounds_per_epoch=3,
    availability_lead=2,
    wins_required=1,
    k_min=1,
    k_max=5,
    claim_window=64,
    opponent_cooldown=1,
    seal_bits=0,
    target_block_seconds=60,
    retarget_interval=10,
    subsidy=coins_to_units(50),
    coinbase_maturity=20,
    min_bond=coins_to_units(10),
    registration_epochs=10,
    unbond_delay=50,
    min_fee=1_000,
    challenge_window=30,
    lag_grace_ms=250,
    timeout_dispute_window=2,
    min_timeout_bond=coins_to_units(1),
    clock_witness_quorum=1,
    clock_witness_bond=coins_to_units(1),
    min_plies=20,
    max_plies=300,
    nodes_per_move=50_000,
    book_version="dev-1",
    quality_mode=QualityMode.OFF,
    quality_sample_rate=0,
    beacon_grace_blocks=2,
    max_block_bytes=1_048_576,
    max_pgn_bytes=262_144,
    max_tx_bytes=65_536,
)

MAINNET: Final = NetworkParams(
    network_id="mainnet",
    address_hrp="sccp",
    epoch_length=20,
    rounds_per_epoch=5,
    availability_lead=2,
    wins_required=3,
    k_min=1,
    k_max=10,
    claim_window=144,
    opponent_cooldown=4,
    seal_bits=16,
    target_block_seconds=600,
    retarget_interval=40,
    subsidy=coins_to_units(50),
    halving_interval=210_000,
    max_supply=21_000_000,
    coinbase_maturity=100,
    min_bond=coins_to_units(500),
    registration_epochs=30,
    unbond_delay=1_000,
    min_fee=1_000,
    challenge_window=100,
    lag_grace_ms=250,
    timeout_dispute_window=6,
    min_timeout_bond=coins_to_units(10),
    clock_witness_quorum=3,
    clock_witness_bond=coins_to_units(50),
    min_plies=30,
    max_plies=400,
    nodes_per_move=250_000,
    book_version="v1",
    quality_mode=QualityMode.SAMPLED,
    quality_sample_rate=50,
    beacon_grace_blocks=6,
    drand_chain="quicknet",
    drand_period_seconds=3,
    drand_round_offset=1,
    max_block_bytes=1_048_576,
    max_pgn_bytes=262_144,
    max_tx_bytes=65_536,
)

DEVNET: Final = NetworkParams.model_validate(
    {
        **TESTNET.model_dump(),
        "network_id": "devnet",
        # Fully deterministic: one-block epochs, no retargeting, fixed genesis time.
        "epoch_length": 1,
        "target_block_seconds": 1,
        "retarget_enabled": False,
        "wins_required": 1,
        "min_bond": 0,
        "genesis_timestamp": 1_700_000_000,
    }
)
"""In-process network for tests and CI: mock engines, zero seal difficulty."""

NETWORKS: Final[dict[str, NetworkParams]] = {
    params.network_id: params for params in (TESTNET, MAINNET, DEVNET)
}


def get_network(network: str | NetworkParams) -> NetworkParams:
    """Resolve a network id (or pass a :class:`NetworkParams` through)."""
    if isinstance(network, NetworkParams):
        return network
    try:
        return NETWORKS[network]
    except KeyError:
        known = ", ".join(sorted(NETWORKS))
        raise NetworkError(f"unknown network {network!r}; known networks: {known}") from None


def network_ids() -> tuple[str, ...]:
    """All known network ids, sorted."""
    return tuple(sorted(NETWORKS))
