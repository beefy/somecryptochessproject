"""The relay follows the chain's epoch (`matchmaker run --node`)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from sccp.errors import RelayError
from sccp.miner.epoch_follower import EpochAdvance, EpochFollower
from sccp.miner.matchmaker import Matchmaker
from sccp.protocol.beacon import compute_beacon

EPOCH = 3
"""The epoch the conftest match and its beacon belong to."""


def _engines(signed_match) -> dict[str, str]:
    return {
        account.address: f"mock-v1:{index:016d}"
        for index, account in enumerate(signed_match.accounts.values(), start=1)
    }


def _relay(signed_match) -> Matchmaker:
    return Matchmaker(
        signed_match.params,
        beacon=signed_match.beacon,
        roster=signed_match.roster,
        public_keys=signed_match.public_keys,
        engines=_engines(signed_match),
        book=signed_match.book,
    )


class _StubNode:
    """A node that serves one canned document per epoch."""

    def __init__(self, *, height: int, views: Mapping[int, Any] | None = None) -> None:
        self.height = height
        self.views = dict(views or {})
        self.asked: list[int] = []

    def status(self) -> Mapping[str, object]:
        return {"height": self.height}

    def epoch(self, epoch: int) -> Mapping[str, object]:
        self.asked.append(epoch)
        if epoch not in self.views:
            raise RelayError(f"GET /v1/epochs/{epoch} -> 404: epoch {epoch} has not been opened")
        return self.views[epoch]


def _beacon(params, epoch: int):
    return compute_beacon(
        network=params.network_id,
        epoch=epoch,
        block_hash=f"{epoch:02x}" * 32,
        reveals=[],
    )


_DEFAULT = object()


def _member(signed_match, address: str, *, engine: object = _DEFAULT) -> dict[str, object]:
    account = next(item for item in signed_match.accounts.values() if item.address == address)
    document: dict[str, object] = {"public_key": account.keypair.public_key_hex}
    fingerprint = _engines(signed_match)[address] if engine is _DEFAULT else engine
    if fingerprint is not None:
        document["engine"] = fingerprint
    document["max_games"] = 2
    return document


def _view(
    signed_match,
    epoch: int,
    *,
    roster: Mapping[str, object] | None = None,
    quotas: Mapping[str, int] | None = None,
    matchable: bool = True,
) -> dict[str, object]:
    members = (
        {address: _member(signed_match, address) for address in signed_match.public_keys}
        if roster is None
        else roster
    )
    return {
        "epoch": epoch,
        "network": signed_match.params.network_id,
        "beacon": _beacon(signed_match.params, epoch).model_dump(mode="json"),
        "roster_root": "00" * 32,
        "roster": members,
        "availability": {},
        "quotas": dict(quotas or {}),
        "matchable": matchable,
        "ended_at_height": None,
    }


def _follower(signed_match, node: _StubNode) -> tuple[EpochFollower, Matchmaker]:
    state = _relay(signed_match)
    return EpochFollower(params=signed_match.params, matchmaker=state, node=node), state


def test_missing_height_is_refused(signed_match) -> None:
    node = _StubNode(height=0)
    node.status = lambda: {"network": "devnet"}  # type: ignore[method-assign]
    follower, _ = _follower(signed_match, node)
    with pytest.raises(RelayError):
        follower.step()


def test_relay_epoch_does_not_ask_the_node_for_a_roster(signed_match) -> None:
    node = _StubNode(height=EPOCH * signed_match.params.epoch_length)
    follower, state = _follower(signed_match, node)
    assert follower.epoch == EPOCH
    assert follower.step() is None
    assert node.asked == []
    assert state.beacon.epoch == EPOCH


def test_unopened_epoch_waits_for_the_next_poll(signed_match) -> None:
    node = _StubNode(height=(EPOCH + 1) * signed_match.params.epoch_length)
    follower, state = _follower(signed_match, node)
    assert follower.step() is None
    assert node.asked == [EPOCH + 1]
    assert state.beacon.epoch == EPOCH


def test_matchable_epoch_moves_the_beacon_and_roster(signed_match) -> None:
    miners = len(signed_match.public_keys)
    document = _view(
        signed_match, EPOCH + 1, quotas={address: 4 for address in signed_match.public_keys}
    )
    node = _StubNode(
        height=(EPOCH + 1) * signed_match.params.epoch_length, views={EPOCH + 1: document}
    )
    follower, state = _follower(signed_match, node)
    advance = follower.step()
    assert advance == EpochAdvance(epoch=EPOCH + 1, miners=miners)
    assert advance.describe() == f"epoch {EPOCH + 1} with {miners} matchable miner(s)"
    assert follower.epoch == EPOCH + 1
    assert state.beacon.epoch == EPOCH + 1
    assert state.beacon.hash == _beacon(signed_match.params, EPOCH + 1).hash
    assert set(state.roster) == set(signed_match.public_keys)
    assert {entry.max_games for entry in state.roster.values()} == {4}
    # A second poll on the same tip has nothing left to do.
    assert follower.step() is None
    assert node.asked == [EPOCH + 1]


def test_quotas_default_to_the_member_max_games(signed_match) -> None:
    document = _view(signed_match, EPOCH + 1)
    node = _StubNode(
        height=(EPOCH + 1) * signed_match.params.epoch_length, views={EPOCH + 1: document}
    )
    follower, state = _follower(signed_match, node)
    assert follower.step() is not None
    assert {entry.max_games for entry in state.roster.values()} == {2}


def test_advance_restarts_round_numbering(signed_match) -> None:
    state = _relay(signed_match)
    state.open_round(0)
    assert state.current_round == 0
    node = _StubNode(
        height=(EPOCH + 1) * signed_match.params.epoch_length,
        views={EPOCH + 1: _view(signed_match, EPOCH + 1)},
    )
    follower = EpochFollower(params=signed_match.params, matchmaker=state, node=node)
    assert follower.step() is not None
    # Round 0 of the new epoch is unseen, and the previous epoch's session is
    # still on record for its receipts.
    assert state.current_round is None
    assert len(state.matches()) == 1
    assert state.open_round(0)


def test_unmatchable_epoch_is_skipped(signed_match) -> None:
    document = _view(signed_match, EPOCH + 1, matchable=False)
    node = _StubNode(
        height=(EPOCH + 1) * signed_match.params.epoch_length, views={EPOCH + 1: document}
    )
    follower, state = _follower(signed_match, node)
    assert follower.step() is None
    assert state.beacon.epoch == EPOCH


def test_epoch_without_a_seated_pair_is_skipped(signed_match) -> None:
    addresses = sorted(signed_match.public_keys)
    roster = {
        addresses[0]: _member(signed_match, addresses[0]),
        # No engine fingerprint: the relay cannot put this miner in a certificate.
        addresses[1]: _member(signed_match, addresses[1], engine=None),
    }
    document = _view(signed_match, EPOCH + 1, roster=roster)
    node = _StubNode(
        height=(EPOCH + 1) * signed_match.params.epoch_length, views={EPOCH + 1: document}
    )
    follower, state = _follower(signed_match, node)
    assert follower.step() is None
    assert state.beacon.epoch == EPOCH


def test_zero_quota_drops_a_miner_from_the_roster(signed_match) -> None:
    addresses = sorted(signed_match.public_keys)
    document = _view(signed_match, EPOCH + 1, quotas={addresses[0]: 0, addresses[1]: 0})
    node = _StubNode(
        height=(EPOCH + 1) * signed_match.params.epoch_length, views={EPOCH + 1: document}
    )
    follower, state = _follower(signed_match, node)
    assert follower.step() is None
    assert state.beacon.epoch == EPOCH


def test_epoch_document_must_describe_the_requested_epoch(signed_match) -> None:
    document = _view(signed_match, EPOCH + 2)
    follower, _ = _follower(signed_match, _StubNode(height=0))
    with pytest.raises(RelayError):
        follower.apply(EPOCH + 1, document)


def test_beacon_must_belong_to_the_epoch(signed_match) -> None:
    document = _view(signed_match, EPOCH + 1)
    document["beacon"] = _beacon(signed_match.params, EPOCH + 2).model_dump(mode="json")
    follower, _ = _follower(signed_match, _StubNode(height=0))
    with pytest.raises(RelayError):
        follower.apply(EPOCH + 1, document)


def test_epoch_without_a_roster_is_refused(signed_match) -> None:
    document = _view(signed_match, EPOCH + 1)
    del document["roster"]
    follower, _ = _follower(signed_match, _StubNode(height=0))
    with pytest.raises(RelayError):
        follower.apply(EPOCH + 1, document)


def test_roster_entry_must_parse(signed_match) -> None:
    addresses = sorted(signed_match.public_keys)
    document = _view(
        signed_match,
        EPOCH + 1,
        roster={
            addresses[0]: {"engine": "mock-v1"},
            addresses[1]: _member(signed_match, addresses[1]),
        },
    )
    follower, _ = _follower(signed_match, _StubNode(height=0))
    with pytest.raises(RelayError):
        follower.apply(EPOCH + 1, document)


def test_public_key_must_derive_its_roster_address(signed_match) -> None:
    addresses = sorted(signed_match.public_keys)
    roster = {
        addresses[0]: _member(signed_match, addresses[1]),
        addresses[1]: _member(signed_match, addresses[1]),
    }
    document = _view(signed_match, EPOCH + 1, roster=roster)
    follower, state = _follower(signed_match, _StubNode(height=0))
    with pytest.raises(RelayError):
        follower.apply(EPOCH + 1, document)
    assert state.beacon.epoch == EPOCH


def test_relay_refuses_a_beacon_from_another_network(signed_match) -> None:
    state = _relay(signed_match)
    foreign = _beacon(signed_match.params, EPOCH + 1).model_copy(update={"network": "testnet"})
    with pytest.raises(RelayError):
        state.advance_epoch(
            foreign,
            roster=state.roster,
            public_keys=signed_match.public_keys,
            engines=_engines(signed_match),
        )
    assert state.beacon.epoch == EPOCH


def test_relay_never_moves_backwards(signed_match) -> None:
    state = _relay(signed_match)
    same_epoch = _beacon(signed_match.params, EPOCH)
    assert (
        state.advance_epoch(
            same_epoch,
            roster=state.roster,
            public_keys=signed_match.public_keys,
            engines=_engines(signed_match),
        )
        is False
    )
    assert state.beacon.epoch == EPOCH
