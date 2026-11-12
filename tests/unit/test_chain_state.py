"""Ledger state: epochs, balances, commitments and snapshots (plan 5.11)."""

from __future__ import annotations

import pytest

from sccp.chain.state import (
    Account,
    State,
    epoch_end_height,
    epoch_of,
    epoch_start_height,
    first_matchable_epoch,
)
from sccp.config import DEVNET, MAINNET
from sccp.errors import StateError
from sccp.protocol.models import RosterMember

ALICE = "sccpt1qz4metcfj6spestnykg7dxh5q95nz70jducmanvg"
BOB = "sccpt1qzg85eqh9as0t9z86sq02n7k5ug54cd0uu6mkp0s"


def _state() -> State:
    return State(params=DEVNET, height=0, timestamps=[1_700_000_000])


def test_epoch_arithmetic_uses_floor_division() -> None:
    assert epoch_of(0, DEVNET) == 0
    assert epoch_of(3, DEVNET) == 3
    assert epoch_of(4, MAINNET) == 0
    assert epoch_of(40, MAINNET) == 2
    assert epoch_start_height(2, MAINNET) == 40
    assert epoch_end_height(2, MAINNET) == 59
    assert epoch_end_height(0, DEVNET) == 0
    assert first_matchable_epoch(DEVNET) == DEVNET.availability_lead
    for helper in (epoch_of, epoch_start_height):
        with pytest.raises(StateError):
            helper(-1, DEVNET)


def test_state_rejects_negative_height_and_minted() -> None:
    with pytest.raises(StateError, match="height"):
        State(params=DEVNET, height=-1)
    with pytest.raises(StateError, match="minted"):
        State(params=DEVNET, minted=-1)


def test_account_defaults_and_rejections() -> None:
    assert Account().total == 0
    with pytest.raises(StateError):
        Account(balance=-1)
    with pytest.raises(StateError):
        Account(nonce=True)  # type: ignore[arg-type]


def test_account_lookup_does_not_store_until_ensure() -> None:
    state = _state()
    assert state.account(ALICE).balance == 0
    assert ALICE not in state.accounts
    state.ensure(ALICE).balance = 5
    assert state.account(ALICE).balance == 5


def test_state_root_tracks_balances() -> None:
    state = _state()
    empty = state.state_root()
    state.ensure(ALICE).balance = 1
    first = state.state_root()
    assert first != empty
    state.ensure(BOB).balance = 1
    assert state.state_root() != first
    state.accounts[ALICE].balance = 0
    state.accounts[BOB].balance = 0
    assert state.state_root() != empty, "a zeroed leaf is still a leaf"


def test_credit_coinbase_matures_after_the_configured_delay() -> None:
    state = _state()
    state.credit_coinbase(ALICE, 1_000, 10)
    assert state.account(ALICE).immature == 1_000
    assert state.account(ALICE).balance == 0
    assert state.minted == 1_000
    maturity = DEVNET.coinbase_maturity
    assert state.release_matured(10 + maturity - 1) == 0
    assert state.release_matured(10 + maturity) == 1_000
    assert state.account(ALICE).balance == 1_000
    assert state.account(ALICE).immature == 0
    assert state.spendable(ALICE) == 1_000
    assert state.release_matured(10 + maturity + 1) == 0


def test_credit_coinbase_ignores_zero_and_rejects_negative() -> None:
    state = _state()
    state.credit_coinbase(ALICE, 0, 1)
    assert state.minted == 0
    with pytest.raises(StateError):
        state.credit_coinbase(ALICE, -1, 1)


def test_open_epoch_records_an_unmatchable_early_epoch() -> None:
    state = _state()
    record = state.open_epoch(0, beacon=None, height=0)
    assert record.matchable is False
    assert record.roster == {}
    assert state.roster_for(0) == {}
    assert record.roster_root == State.roster_root_of({})


def test_open_epoch_closes_the_previous_epoch() -> None:
    state = _state()
    state.open_epoch(0, beacon=None, height=0)
    second = state.open_epoch(1, beacon=None, height=1)
    assert state.epochs[0].ended_at_height == 0
    assert second.ended_at_height is None


def test_matchable_requires_registration_bond_and_key() -> None:
    state = _state()
    assert state.matchable(ALICE, 3) is False
    account = state.ensure(ALICE)
    account.public_key = "11" * 32
    assert state.matchable(ALICE, 3) is False, "no registration yet"
    account.registered_until_epoch = 5
    assert state.matchable(ALICE, 3) is True
    assert state.matchable(ALICE, 6) is False
    account.banned_until = 10
    assert state.matchable(ALICE, 3) is False


def test_roster_root_commits_key_and_quota() -> None:
    def root(key: str, games: int) -> str:
        return State.roster_root_of(
            {ALICE: RosterMember(public_key=key, engine="mock-v1", max_games=games)}
        )

    assert root("11" * 32, 3) != root("11" * 32, 4)
    assert root("11" * 32, 3) != root("22" * 32, 3)
    assert root("11" * 32, 3) == root("11" * 32, 3)


def test_snapshot_round_trip_preserves_the_ledger() -> None:
    state = _state()
    state.ensure(ALICE).balance = 42
    state.credit_coinbase(ALICE, 7, 0)
    state.open_epoch(0, beacon=None, height=0)
    state.tip_hash = "ab" * 32
    restored = State.from_snapshot(DEVNET, state.snapshot())
    assert restored.state_root() == state.state_root()
    assert restored.minted == 7
    assert sorted(restored.epochs) == [0]
    assert restored.tip_hash == "ab" * 32
    assert restored.timestamps == state.timestamps
    assert restored.account(ALICE).immature == 7


def test_copy_is_independent() -> None:
    state = _state()
    copy = state.copy()
    copy.ensure(ALICE).balance = 1
    copy.height = 9
    assert state.account(ALICE).balance == 0
    assert state.height == 0


def test_from_snapshot_rejects_a_foreign_network() -> None:
    state = _state()
    with pytest.raises(StateError, match="snapshot is for 'devnet'"):
        State.from_snapshot(MAINNET, state.snapshot())
