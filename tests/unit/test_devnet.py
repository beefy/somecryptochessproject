"""The in-process devnet: a whole network in one call (M4)."""

from __future__ import annotations

import json

import pytest

from sccp.devnet import GENESIS_EPOCH, run_devnet
from sccp.errors import DevnetError


def test_devnet_reaches_the_requested_height() -> None:
    summary = run_devnet(miners=2, blocks=4)
    assert summary.network == "devnet"
    assert summary.miners == 2
    assert summary.height == 4
    assert summary.epochs == (GENESIS_EPOCH,)
    assert len(summary.matches) == 3
    assert summary.proofs
    assert all(balance > 0 for balance in summary.balances.values())
    assert summary.seconds >= 0


def test_devnet_is_deterministic() -> None:
    first = run_devnet(miners=2, blocks=4)
    second = run_devnet(miners=2, blocks=4)
    assert first.tip_hash == second.tip_hash
    assert first.proofs == second.proofs
    assert first.matches == second.matches


def test_devnet_refuses_impossible_requests() -> None:
    with pytest.raises(DevnetError, match="at least two"):
        run_devnet(miners=1)
    with pytest.raises(DevnetError, match="at least one block"):
        run_devnet(blocks=0)


def test_summary_is_json_friendly() -> None:
    summary = run_devnet(miners=2, blocks=4)
    payload = json.loads(json.dumps(summary.as_dict(), sort_keys=True))
    assert payload["height"] == 4
    assert payload["epochs"] == [GENESIS_EPOCH]
    assert payload["tip_hash"] == summary.tip_hash
