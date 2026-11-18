"""Immutable network parameters (docs/design/PROTOCOL.md section 15).

Amounts are integer base units: ``1 coin = 100_000_000 units``. No floating
point value is ever accepted by this module. ``NetworkParams`` validates its own
invariants at construction, so an impossible network definition fails at import
time rather than at block validation time.
"""

from __future__ import annotations

from typing import Final, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from sccp.errors import ConfigError, NetworkError
from sccp.serialization import Domain, domain_hex

UNITS_PER_COIN: Final = 100_000_000
"""Base units per coin. All consensus amounts are integers of this unit."""

STANDARD_VARIANT: Final = "standard"
"""The native SCCP variant. It is first in every network's variant list."""

CHESS960_VARIANT: Final = "chess960"
"""Fischer Random: the same pieces, a shuffled back rank and 960 start positions."""

CHESS960_ASSET_ID: Final = domain_hex(
    Domain.ASSET,
    {
        "symbol": "C960",
        "name": "Chess960",
        "decimals": 8,
        "variant": CHESS960_VARIANT,
    },
)
"""The immutable asset id of the Chess960 token (PROTOCOL.md section 5.4).

The id is derived from the asset's parameters rather than assigned by an issuer,
so two nodes cannot disagree about which asset a C960 transfer names.
"""

MAX_PLIES_LIMIT: Final = 1000
"""Hard upper bound for ``max_plies``; longer games are never consensus-valid."""

CLOCK_EVIDENCE_PLIES: Final = 64
"""Most move receipts a clock-evidence bundle may carry (PROTOCOL.md section 8.4).

A flag claim proves the tail of the game, not the whole game: the window starts at
a witness-signed receipt whose absolute clock values anchor the arithmetic, and
every later receipt is checked as a delta from it. Bounding the window is what
keeps the largest transaction the protocol can produce well inside
``max_tx_bytes``; before it existed a 400-ply broadcast flag bundle overflowed the
transaction cap with classical signatures alone.
"""


def coins_to_units(coins: int) -> int:
    """Convert whole coins to base units."""
    if not isinstance(coins, int) or isinstance(coins, bool):
        raise ConfigError("coin amounts must be integers")
    return coins * UNITS_PER_COIN


def format_coins(units: int) -> str:
    """Format base units as fixed-point coins for a human; never a float.

    A bare ``1200001000`` in an error message reads like an amount of coins, so
    every user-facing message prints through this instead: ``12.00001000``.
    """
    whole, fraction = divmod(abs(units), UNITS_PER_COIN)
    sign = "-" if units < 0 else ""
    return f"{sign}{whole}.{fraction:08d}"


class VariantParams(BaseModel):
    """One chess variant and the asset its settled proofs mint.

    Standard chess is the network's native asset: ``asset`` is ``None`` and the
    reward is SCCP. Every other variant names a derived asset id (see
    :data:`CHESS960_ASSET_ID`) and mints only that asset.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1, max_length=32)
    rules: str = Field(min_length=1, max_length=32)
    asset: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    symbol: str = Field(min_length=1, max_length=16)
    display_name: str = Field(min_length=1, max_length=64)
    decimals: int = Field(default=8, ge=0, le=18)
    subsidy: int = Field(gt=0)
    halving_interval: int | None = Field(default=None, ge=1)
    max_supply: int | None = Field(default=None, ge=1)
    min_plies: int = Field(ge=1)
    max_games_per_block: int = Field(default=1, ge=1)
    opening_book: str = Field(default="standard", min_length=1, max_length=64)

    @property
    def native(self) -> bool:
        """True when proofs of this variant mint the network's native coin."""
        return self.asset is None

    @model_validator(mode="after")
    def _check_invariants(self) -> Self:
        if self.rules not in ("chess", "chess960"):
            raise ValueError(f"unsupported variant rules {self.rules!r}")
        if self.name == STANDARD_VARIANT and (self.asset is not None or self.rules != "chess"):
            raise ValueError("the standard variant is native SCCP with standard chess rules")
        if self.name != STANDARD_VARIANT and self.asset is None:
            raise ValueError(f"variant {self.name!r} needs a derived asset id")
        if (self.max_supply is None) != (self.halving_interval is None):
            raise ValueError("a variant sets max_supply and halving_interval together")
        return self


CHESS960: Final = VariantParams(
    name=CHESS960_VARIANT,
    rules="chess960",
    asset=CHESS960_ASSET_ID,
    symbol="C960",
    display_name="Chess960",
    subsidy=coins_to_units(50),
    halving_interval=210_000,
    max_supply=21_000_000,
    min_plies=20,
    max_games_per_block=1,
    opening_book="none",
)
"""Fischer Random with the same issuance schedule as SCCP, in its own asset."""


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
    dex_fee_bps: int = Field(default=30, ge=0, le=1_000)
    """Constant-product swap fee, in basis points, paid to liquidity providers."""

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

    # Variants and their assets. The validator always prepends standard chess,
    # so an empty tuple means "native standard chess only".
    variants: tuple[VariantParams, ...] = ()

    # Beacon
    beacon_grace_blocks: int = Field(ge=0)
    drand_chain: str | None = None
    drand_period_seconds: int | None = Field(default=None, ge=1)
    drand_round_offset: int | None = None

    # Size caps
    max_block_bytes: int = Field(gt=0)
    max_pgn_bytes: int = Field(gt=0)
    max_tx_bytes: int = Field(gt=0)

    # Ledger: pinned genesis and storage policy
    genesis_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    mempool_ttl_blocks: int = Field(default=64, ge=1)
    snapshot_interval: int = Field(default=100, ge=1)
    reorg_depth: int = Field(default=10, ge=1)

    @property
    def max_money(self) -> int | None:
        """Supply cap in base units, or ``None`` when the network is uncapped."""
        if self.max_supply is None:
            return None
        return self.max_supply * UNITS_PER_COIN

    def variant(self, name: str) -> VariantParams:
        """Resolve a variant by name, or raise :class:`ConfigError`."""
        for candidate in self.variants:
            if candidate.name == name:
                return candidate
        known = ", ".join(candidate.name for candidate in self.variants)
        raise ConfigError(f"unknown variant {name!r}; this network has: {known}")

    def variant_names(self) -> tuple[str, ...]:
        """Every variant this network settles, standard first."""
        return tuple(candidate.name for candidate in self.variants)

    def variant_for_asset(self, asset: str) -> VariantParams:
        """Resolve the variant that mints ``asset`` (native SCCP is standard)."""
        if asset == "SCCP":
            return self.variant(STANDARD_VARIANT)
        for candidate in self.variants:
            if candidate.asset == asset:
                return candidate
        raise ConfigError(f"unknown asset id {asset!r}")

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

        # Standard chess is the network's own unit of account. It is always
        # first and always mirrors the network's own schedule, even when a
        # caller only lists the optional variants or overrides min_plies.
        supplied = tuple(self.variants)
        standard = VariantParams(
            name=STANDARD_VARIANT,
            rules="chess",
            asset=None,
            symbol="SCCP",
            display_name="SCCP",
            subsidy=self.subsidy,
            halving_interval=self.halving_interval,
            max_supply=self.max_supply,
            min_plies=self.min_plies,
            max_games_per_block=1,
            opening_book=self.book_version,
        )
        variants = (
            standard,
            *(item for item in supplied if item.name != STANDARD_VARIANT),
        )
        names = [variant.name for variant in variants]
        if len(set(names)) != len(names):
            raise ValueError("variant names must be unique")
        assets = [variant.asset for variant in variants if variant.asset is not None]
        if len(set(assets)) != len(assets):
            raise ValueError("two variants cannot mint the same asset")
        object.__setattr__(self, "variants", variants)
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
    # Test networks seat the same small roster over and over; a zero cooldown
    # disables the rematch rule (PROTOCOL.md section 12.2) so every decisive game
    # can settle. Only mainnet enforces a cooldown.
    opponent_cooldown=0,
    seal_bits=0,
    target_block_seconds=60,
    retarget_interval=10,
    subsidy=coins_to_units(50),
    coinbase_maturity=20,
    min_bond=coins_to_units(10),
    registration_epochs=10,
    unbond_delay=50,
    min_fee=1_000,
    lag_grace_ms=250,
    timeout_dispute_window=2,
    min_timeout_bond=coins_to_units(1),
    clock_witness_quorum=1,
    clock_witness_bond=coins_to_units(1),
    min_plies=20,
    max_plies=300,
    nodes_per_move=50_000,
    book_version="dev-1",
    variants=(CHESS960,),
    beacon_grace_blocks=2,
    max_block_bytes=1_048_576,
    max_pgn_bytes=262_144,
    max_tx_bytes=131_072,
    genesis_hash="df754cb036e2f5488ed7d3a3b81a9e09f940a4c50feeddbbab6e6912243eedc7",
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
    lag_grace_ms=250,
    timeout_dispute_window=6,
    min_timeout_bond=coins_to_units(10),
    clock_witness_quorum=3,
    clock_witness_bond=coins_to_units(50),
    min_plies=30,
    max_plies=400,
    nodes_per_move=250_000,
    book_version="v1",
    variants=(CHESS960,),
    beacon_grace_blocks=6,
    drand_chain="quicknet",
    drand_period_seconds=3,
    drand_round_offset=1,
    max_block_bytes=1_048_576,
    max_pgn_bytes=262_144,
    max_tx_bytes=131_072,
    genesis_hash="8e28f57cae3a417de7751d9bd7eddc5feb7ae9b698c81e1a49c20b0ca2da20e7",
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
        "genesis_hash": "5e3465524fbf12874f77384baae18eec299068d96aa85ef8a34a3932b0b5f8a4",
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
