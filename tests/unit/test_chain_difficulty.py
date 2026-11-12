"""Difficulty schedule and the seal target (plan section 5.11)."""

from __future__ import annotations

import pytest

from sccp.chain.difficulty import (
    MAX_SEAL_BITS,
    WIN_UNIT,
    Difficulty,
    block_work,
    expected_interval_seconds,
    initial_difficulty,
    next_difficulty,
    seal_valid,
)
from sccp.config import DEVNET, TESTNET, NetworkParams
from sccp.errors import StateError


def _params(**overrides: object) -> NetworkParams:
    return TESTNET.model_copy(update=overrides)


def _window(params: NetworkParams, start: int, step_seconds: int) -> list[int]:
    return [start + index * step_seconds for index in range(params.retarget_interval)]


def test_work_counts_k_and_the_seal() -> None:
    assert block_work(0, 1) == 1 + WIN_UNIT
    assert block_work(4, 3) == 16 + 3 * WIN_UNIT
    assert Difficulty(wins_required=2, seal_bits=1).work == 2 + 2 * WIN_UNIT
    for bad in (-1, MAX_SEAL_BITS + 1):
        with pytest.raises(StateError):
            block_work(bad, 1)
    with pytest.raises(StateError):
        block_work(0, 0)
    with pytest.raises(StateError):
        Difficulty(wins_required=0, seal_bits=0)


def test_initial_difficulty_reads_the_network() -> None:
    assert initial_difficulty(DEVNET) == Difficulty(
        wins_required=DEVNET.wins_required, seal_bits=DEVNET.seal_bits
    )


def test_seal_target_covers_the_declared_bits() -> None:
    assert seal_valid("ff" * 32, 0) is True
    assert seal_valid("00" + "ff" * 31, 8) is True
    assert seal_valid("01" + "ff" * 31, 8) is False
    with pytest.raises(StateError):
        seal_valid("ff" * 32, MAX_SEAL_BITS + 1)
    with pytest.raises(StateError):
        seal_valid("not-hex", 8)


def test_expected_interval_uses_the_first_and_last_sample() -> None:
    assert expected_interval_seconds(TESTNET) == TESTNET.target_block_seconds * 9


def test_retargeting_is_disabled_for_devnet() -> None:
    parent = Difficulty(wins_required=1, seal_bits=0)
    assert (
        next_difficulty(
            DEVNET,
            parent,
            height=DEVNET.retarget_interval,
            timestamps=_window(DEVNET, 1_700_000_000, 10_000),
        )
        == parent
    )


def test_retargeting_waits_for_a_full_window() -> None:
    parent = Difficulty(wins_required=3, seal_bits=4)
    params = _params()
    assert next_difficulty(params, parent, height=5, timestamps=_window(params, 0, 1)) == parent
    assert (
        next_difficulty(params, parent, height=params.retarget_interval, timestamps=[0] * 5)
        == parent
    )


def test_slow_intervals_lower_both_levers() -> None:
    params = _params()
    parent = Difficulty(wins_required=3, seal_bits=4)
    slow = _window(params, 1_700_000_000, params.target_block_seconds * 5)
    lowered = next_difficulty(params, parent, height=params.retarget_interval, timestamps=slow)
    assert lowered.seal_bits == 3
    assert lowered.wins_required == 2


def test_fast_intervals_raise_both_levers() -> None:
    params = _params()
    parent = Difficulty(wins_required=3, seal_bits=4)
    fast = _window(params, 1_700_000_000, 1)
    raised = next_difficulty(params, parent, height=params.retarget_interval, timestamps=fast)
    assert raised.seal_bits == 5
    assert raised.wins_required == 4


def test_steady_state_is_a_no_op() -> None:
    params = _params()
    parent = Difficulty(wins_required=3, seal_bits=4)
    steady = _window(params, 1_700_000_000, params.target_block_seconds)
    assert (
        next_difficulty(params, parent, height=params.retarget_interval, timestamps=steady)
        == parent
    )


def test_k_moves_only_within_its_bounds_and_the_grace_period() -> None:
    params = _params(k_min=1, k_max=4)
    fast = _window(params, 1_700_000_000, 1)
    moved = next_difficulty(
        params, Difficulty(wins_required=3, seal_bits=4), height=10, timestamps=fast
    )
    assert moved.wins_required == 4
    assert moved.seal_bits == 5

    graced = next_difficulty(
        params,
        Difficulty(wins_required=3, seal_bits=4),
        height=10,
        timestamps=fast,
        last_k_change_height=8,
    )
    assert graced.wins_required == 3, "k may not move twice inside one epoch"
    assert graced.seal_bits == 5

    ceiling = next_difficulty(
        _params(k_min=1, k_max=3),
        Difficulty(wins_required=3, seal_bits=4),
        height=10,
        timestamps=fast,
    )
    assert ceiling.wins_required == 3
    floor = next_difficulty(
        _params(k_min=1, k_max=4),
        Difficulty(wins_required=1, seal_bits=4),
        height=10,
        timestamps=_window(params, 1_700_000_000, params.target_block_seconds * 2),
    )
    assert floor.wins_required == 1
    assert floor.seal_bits == 3


def test_non_monotonic_timestamps_are_rejected() -> None:
    params = _params()
    backwards = _window(params, 1_700_000_000, 1)
    backwards[-1] = backwards[0] - 1
    with pytest.raises(StateError, match="monotonic"):
        next_difficulty(
            params,
            Difficulty(wins_required=3, seal_bits=4),
            height=params.retarget_interval,
            timestamps=backwards,
        )
