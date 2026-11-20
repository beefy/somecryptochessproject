"""Constant-product liquidity pools (PROTOCOL.md section 10.6).

The DEX is consensus state, not a browser-side order book. A pool is an ordered
pair of assets, two reserves and a share ledger; a swap is one signed transaction
that moves both assets in the same state transition. That makes the price and the
LP claim as auditable as a transfer, and it means a client cannot fake a fill.

The native SCCP side is the empty string in state and protocol payloads. It is
the same convention a native ``transfer`` uses by omitting ``asset``: only the
other side of a pair ever needs an asset id. Pools are ordered by asset key, so
``pool_id`` is a pure function of the pair and both nodes derive the same id.

The curve is ``x * y = k`` with a proportional fee:

``amount_out = amount_in * (10000 - fee_bps) * reserve_out /
               (reserve_in * 10000 + amount_in * (10000 - fee_bps))``

Integer division rounds in the pool's favour. The fee stays in the input reserve,
which is what pays liquidity providers.
"""

from __future__ import annotations

from math import isqrt
from typing import Final

from sccp.errors import StateError
from sccp.serialization import Domain, domain_hex

NATIVE_ASSET: Final = ""
"""The native SCCP side of a pair; any 64-hex asset id names a variant token."""

BPS_DENOMINATOR: Final = 10_000


def normalize_pair(
    asset_x: str, amount_x: int, asset_y: str, amount_y: int
) -> tuple[str, int, str, int]:
    """Order a pair by asset key, carrying each amount with its side."""
    if asset_x == asset_y:
        raise StateError("a pool needs two different assets")
    if asset_x > asset_y:
        return asset_y, amount_y, asset_x, amount_x
    return asset_x, amount_x, asset_y, amount_y


def pool_id(asset_x: str, asset_y: str) -> str:
    """The deterministic id of a pool pair, independent of side order."""
    first, second = sorted((asset_x, asset_y))
    if first == second:
        raise StateError("a pool needs two different assets")
    return domain_hex(Domain.POOL, {"asset_a": first, "asset_b": second})


def initial_shares(amount_a: int, amount_b: int) -> int:
    """``isqrt(amount_a * amount_b)`` for a pool's first deposit."""
    if amount_a <= 0 or amount_b <= 0:
        return 0
    return isqrt(amount_a * amount_b)


def added_shares(
    reserve_a: int,
    reserve_b: int,
    total_shares: int,
    amount_a: int,
    amount_b: int,
) -> int:
    """Shares for a deposit, bounded by the scarcer side of the pair.

    A deposit that is not exactly proportional credits the minimum of the two
    implied share counts. The excess stays in the pool: the chain cannot refund it
    without a second transfer, and silently crediting it would let a depositor
    mint shares out of an imbalanced pair. The wallet pre-fills the exact ratio.
    """
    if amount_a <= 0 or amount_b <= 0 or total_shares <= 0:
        return 0
    if reserve_a <= 0 or reserve_b <= 0:
        return 0
    by_a = amount_a * total_shares // reserve_a
    by_b = amount_b * total_shares // reserve_b
    return min(by_a, by_b)


def removed_amounts(
    reserve_a: int,
    reserve_b: int,
    total_shares: int,
    shares: int,
) -> tuple[int, int]:
    """The two reserves a share redemption pays out, rounded down."""
    if shares <= 0 or total_shares <= 0:
        return 0, 0
    return reserve_a * shares // total_shares, reserve_b * shares // total_shares


def swap_output(
    reserve_in: int,
    reserve_out: int,
    amount_in: int,
    fee_bps: int,
) -> int:
    """The output of a fee-paying constant-product swap, rounded down."""
    if amount_in <= 0 or reserve_in <= 0 or reserve_out <= 0:
        return 0
    if not 0 <= fee_bps < BPS_DENOMINATOR:
        raise StateError(f"swap fee {fee_bps} is outside [0, {BPS_DENOMINATOR})")
    amount_after_fee = amount_in * (BPS_DENOMINATOR - fee_bps)
    numerator = amount_after_fee * reserve_out
    denominator = reserve_in * BPS_DENOMINATOR + amount_after_fee
    return numerator // denominator


def proportional_amount(
    amount_a: int,
    reserve_a: int,
    reserve_b: int,
) -> int:
    """The B side that keeps ``amount_a`` at the pool's current ratio.

    Rounded up, so the deposit clears ``added_shares``' proportional test rather
    than falling a unit short. This is a quoting helper for clients; consensus
    validates the two amounts it is given.
    """
    if amount_a <= 0 or reserve_a <= 0:
        return 0
    return (amount_a * reserve_b + reserve_a - 1) // reserve_a


__all__ = [
    "BPS_DENOMINATOR",
    "NATIVE_ASSET",
    "added_shares",
    "initial_shares",
    "normalize_pair",
    "pool_id",
    "proportional_amount",
    "removed_amounts",
    "swap_output",
]
