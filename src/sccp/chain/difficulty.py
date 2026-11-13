"""Difficulty scheduling: ``wins_required`` (k) and ``seal_bits`` (plan 5.11).

The chain's *real* cost is playing ``k`` verified chess games, so ``seal_bits``
only absorbs residual variance and is retargeted in single-bit steps. ``k`` is
the coarse lever and may only move on an epoch boundary.

Everything here is integer arithmetic and a pure function of the parent's
timestamps and difficulty, because every node must recompute the same schedule
and reject a header that declares easier values than the schedule allows.

Testnet and devnet run with ``seal_bits = 0`` and retargeting disabled, so the
seal costs nothing and block spacing is whatever the chess takes -- expected, not
a bug (PROTOCOL.md sections 12-13).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from sccp.config import NetworkParams
from sccp.errors import StateError

WIN_UNIT: Final = 1 << 16
"""Work credited per required win; one unit of ``k`` (PROTOCOL.md sections 12-13)."""

MAX_SEAL_BITS: Final = 32
"""Hard ceiling for the header seal target."""

MIN_SEAL_BITS: Final = 0
"""Zero seal bits means every header hash is valid."""

SLOW_NUMERATOR: Final = 4
SLOW_DENOMINATOR: Final = 3
"""Retarget down when the interval took more than 4/3 of the expected time."""

FAST_NUMERATOR: Final = 3
FAST_DENOMINATOR: Final = 4
"""Retarget up when the interval took less than 3/4 of the expected time."""


@dataclass(frozen=True, slots=True)
class Difficulty:
    """The two declared difficulty knobs of one block header."""

    wins_required: int
    seal_bits: int

    def __post_init__(self) -> None:
        if self.wins_required < 1:
            raise StateError(f"wins_required must be at least 1, got {self.wins_required}")
        if not 0 <= self.seal_bits <= MAX_SEAL_BITS:
            raise StateError(f"seal_bits must be in [0, {MAX_SEAL_BITS}], got {self.seal_bits}")

    @property
    def work(self) -> int:
        """``(1 << seal_bits) + k * WIN_UNIT`` -- the work a header declares.

        Fork choice does not use this directly: it credits
        :func:`earned_block_work`, which drops the chess term when the block
        does not carry the proofs that term is paid for.
        """
        return block_work(self.seal_bits, self.wins_required)

    def as_dict(self) -> dict[str, int]:
        """Canonical mapping, used for state snapshots and merkle commitments."""
        return {"wins_required": self.wins_required, "seal_bits": self.seal_bits}


def block_work(seal_bits: int, wins_required: int) -> int:
    """Cumulative-work contribution of one block (documented v1 approximation)."""
    if seal_bits < 0 or seal_bits > MAX_SEAL_BITS:
        raise StateError(f"seal_bits must be in [0, {MAX_SEAL_BITS}], got {seal_bits}")
    if wins_required < 1:
        raise StateError(f"wins_required must be at least 1, got {wins_required}")
    return (1 << seal_bits) + wins_required * WIN_UNIT


def earned_block_work(seal_bits: int, wins_required: int, proof_count: int) -> int:
    """Fork-choice credit for a block, counting only work it actually carries.

    :func:`block_work` is what a header *declares*; this is what the block
    *earns*. The ``wins_required * WIN_UNIT`` chess term is credited only when
    the block carries the proofs it owes. A bootstrap block and a proofless
    *heartbeat* block carry none, so each earns its seal work alone.

    Crediting the chess term regardless would let a branch of proofless blocks
    claim work nobody performed and outrun a branch of the same length that
    carries real proofs -- which is the whole cost of extending a fork.
    """
    declared = block_work(seal_bits, wins_required)
    if proof_count < wins_required:
        return declared - wins_required * WIN_UNIT
    return declared


def initial_difficulty(params: NetworkParams) -> Difficulty:
    """The difficulty every network starts with, from its parameters."""
    return Difficulty(wins_required=params.wins_required, seal_bits=params.seal_bits)


def expected_interval_seconds(params: NetworkParams) -> int:
    """Expected wall-clock span of one full retarget interval, in seconds.

    Block timestamps are Unix seconds, so the comparison window is in seconds
    too. The interval's *first to last* spacing is ``target_block_seconds *
    (retarget_interval - 1)``; comparing against that (rather than
    ``retarget_interval``) is what makes the steady state a no-op.
    """
    return params.target_block_seconds * (params.retarget_interval - 1)


def next_difficulty(
    params: NetworkParams,
    current: Difficulty,
    *,
    height: int,
    timestamps: Sequence[int],
    last_k_change_height: int | None = None,
) -> Difficulty:
    """The difficulty a block at ``height`` must declare.

    ``timestamps`` is the tail of the parent chain's block timestamps, oldest
    first. A retarget is evaluated only when ``height`` is a multiple of
    ``retarget_interval`` and there are enough samples; otherwise the parent's
    difficulty carries forward unchanged.

    ``k`` moves at most once per epoch (``height % epoch_length == 0``) and never
    within ``epoch_length`` blocks of the previous ``k`` change, which is the
    "one epoch of grace" the protocol requires for open claim windows.
    """
    if not params.retarget_enabled:
        return current
    if height <= 0 or height % params.retarget_interval != 0:
        return current
    if len(timestamps) < params.retarget_interval:
        return current

    window = list(timestamps)[-params.retarget_interval :]
    expected = expected_interval_seconds(params)
    actual = window[-1] - window[0]
    if actual < 0:
        raise StateError("block timestamps are not monotonic over the retarget window")

    seal_bits = current.seal_bits
    wins_required = current.wins_required
    moved_k = False

    if actual * SLOW_DENOMINATOR > expected * SLOW_NUMERATOR:
        seal_bits = max(MIN_SEAL_BITS, seal_bits - 1)
        if _k_change_allowed(params, height, last_k_change_height):
            wins_required = max(params.k_min, wins_required - 1)
            moved_k = wins_required != current.wins_required
    elif actual * FAST_DENOMINATOR < expected * FAST_NUMERATOR:
        seal_bits = min(MAX_SEAL_BITS, seal_bits + 1)
        if _k_change_allowed(params, height, last_k_change_height):
            wins_required = min(params.k_max, wins_required + 1)
            moved_k = wins_required != current.wins_required
    else:
        return current

    if seal_bits == current.seal_bits and not moved_k:
        return current
    return Difficulty(wins_required=wins_required, seal_bits=seal_bits)


def _k_change_allowed(
    params: NetworkParams,
    height: int,
    last_k_change_height: int | None,
) -> bool:
    """``k`` may only move on an epoch boundary, with one epoch of grace."""
    if height % params.epoch_length != 0:
        return False
    if last_k_change_height is None:
        return True
    return height - last_k_change_height >= params.epoch_length


def seal_valid(block_hash: str, seal_bits: int) -> bool:
    """``int(hash, 16) < 2 ** (256 - seal_bits)``; always true for 0 bits."""
    if seal_bits < 0 or seal_bits > MAX_SEAL_BITS:
        raise StateError(f"seal_bits must be in [0, {MAX_SEAL_BITS}], got {seal_bits}")
    if seal_bits == 0:
        return True
    try:
        value = int(block_hash, 16)
    except ValueError as exc:
        raise StateError(f"block hash is not hexadecimal: {block_hash!r}") from exc
    return value < (1 << (256 - seal_bits))


__all__ = [
    "FAST_DENOMINATOR",
    "FAST_NUMERATOR",
    "MAX_SEAL_BITS",
    "MIN_SEAL_BITS",
    "SLOW_DENOMINATOR",
    "SLOW_NUMERATOR",
    "WIN_UNIT",
    "Difficulty",
    "block_work",
    "earned_block_work",
    "expected_interval_seconds",
    "initial_difficulty",
    "next_difficulty",
    "seal_valid",
]
