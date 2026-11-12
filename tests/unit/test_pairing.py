"""Deterministic pairing, quotas and soft constraints (PROTOCOL.md section 7)."""

from __future__ import annotations

import pytest

import sccp.protocol.pairing as pairing_module
from sccp.errors import PairingError
from sccp.protocol.pairing import (
    RosterEntry,
    match_id,
    pair,
    pairing_score,
    verify_pairing,
    white_seat,
)

BEACON = bytes.fromhex("ab" * 32)
OTHER_BEACON = bytes.fromhex("cd" * 32)


def _roster(count: int, *, max_games: int = 3) -> list[RosterEntry]:
    return [
        RosterEntry(address=f"sccpt1miner{index:02d}", max_games=max_games)
        for index in range(count)
    ]


def _addresses(matchups) -> list[str]:
    return [address for matchup in matchups for address in (matchup.white, matchup.black)]


def test_pairing_is_deterministic_and_roster_order_independent() -> None:
    roster = _roster(6)
    first = pair(
        roster,
        BEACON,
        network="testnet",
        epoch=3,
        round_number=0,
        rounds_per_epoch=3,
    )
    second = pair(
        list(reversed(roster)),
        BEACON,
        network="testnet",
        epoch=3,
        round_number=0,
        rounds_per_epoch=3,
    )
    assert first == second
    assert len(first) == 3
    assert sorted(_addresses(first)) == sorted(entry.address for entry in roster)


def test_changing_the_beacon_reshuffles_the_pairing() -> None:
    roster = _roster(6)
    first = pair(
        roster,
        BEACON,
        network="testnet",
        epoch=3,
        round_number=0,
        rounds_per_epoch=3,
    )
    second = pair(
        roster,
        OTHER_BEACON,
        network="testnet",
        epoch=3,
        round_number=0,
        rounds_per_epoch=3,
    )
    assert first != second


def test_no_seat_is_paired_twice_and_odd_rosters_sit_one_out() -> None:
    roster = _roster(5)
    matchups = pair(
        roster,
        BEACON,
        network="testnet",
        epoch=3,
        round_number=0,
        rounds_per_epoch=3,
    )
    seats = _addresses(matchups)
    assert len(seats) == len(set(seats)) == 4
    assert len(matchups) == 2


def test_quotas_limit_rounds_and_are_strictly_validated() -> None:
    roster = _roster(2, max_games=1)
    first = pair(
        roster,
        BEACON,
        network="testnet",
        epoch=3,
        round_number=0,
        rounds_per_epoch=3,
    )
    assert len(first) == 1
    second = pair(
        roster,
        BEACON,
        network="testnet",
        epoch=3,
        round_number=1,
        rounds_per_epoch=3,
    )
    assert second == []

    explicit = pair(
        roster,
        BEACON,
        network="testnet",
        epoch=3,
        round_number=1,
        rounds_per_epoch=3,
        quotas={roster[0].address: 1, roster[1].address: 1},
    )
    assert len(explicit) == 1

    with pytest.raises(PairingError, match="outside the roster"):
        pair(
            roster,
            BEACON,
            network="testnet",
            epoch=3,
            round_number=0,
            rounds_per_epoch=3,
            quotas={"sccpt1nobody": 1},
        )
    with pytest.raises(PairingError, match="non-negative"):
        pair(
            roster,
            BEACON,
            network="testnet",
            epoch=3,
            round_number=0,
            rounds_per_epoch=3,
            quotas={roster[0].address: -1},
        )


def test_soft_constraints_are_repaired_by_swapping(monkeypatch: pytest.MonkeyPatch) -> None:
    roster = [
        RosterEntry(address="sccpt1aaaa", funder="fund-1"),
        RosterEntry(address="sccpt1bbbb", funder="fund-1"),
        RosterEntry(address="sccpt1cccc"),
        RosterEntry(address="sccpt1dddd"),
    ]
    order = {
        "sccpt1aaaa": b"\x00" * 32,
        "sccpt1bbbb": b"\x01" * 32,
        "sccpt1cccc": b"\x02" * 32,
        "sccpt1dddd": b"\x03" * 32,
    }
    monkeypatch.setattr(
        pairing_module,
        "pairing_score",
        lambda network, beacon, round_number, address: order[address],
    )
    matchups = pair(
        roster,
        BEACON,
        network="testnet",
        epoch=3,
        round_number=0,
        rounds_per_epoch=3,
    )
    pairs = {frozenset((matchup.white, matchup.black)) for matchup in matchups}
    assert frozenset(("sccpt1aaaa", "sccpt1bbbb")) not in pairs
    assert len(_addresses(matchups)) == len(set(_addresses(matchups))) == 4


def test_recent_opponents_are_a_soft_constraint(monkeypatch: pytest.MonkeyPatch) -> None:
    roster = [
        RosterEntry(address="sccpt1aaaa", recent_opponents=("sccpt1bbbb",)),
        RosterEntry(address="sccpt1bbbb", recent_opponents=("sccpt1aaaa",)),
        RosterEntry(address="sccpt1cccc"),
        RosterEntry(address="sccpt1dddd"),
    ]
    order = {entry.address: bytes([index]) * 32 for index, entry in enumerate(roster)}
    monkeypatch.setattr(
        pairing_module,
        "pairing_score",
        lambda network, beacon, round_number, address: order[address],
    )
    matchups = pair(
        roster,
        BEACON,
        network="testnet",
        epoch=3,
        round_number=0,
        rounds_per_epoch=3,
        opponent_cooldown=1,
    )
    pairs = {frozenset((matchup.white, matchup.black)) for matchup in matchups}
    assert frozenset(("sccpt1aaaa", "sccpt1bbbb")) not in pairs

    ignored = pair(
        roster,
        BEACON,
        network="testnet",
        epoch=3,
        round_number=0,
        rounds_per_epoch=3,
        opponent_cooldown=0,
    )
    assert frozenset(("sccpt1aaaa", "sccpt1bbbb")) in {
        frozenset((matchup.white, matchup.black)) for matchup in ignored
    }


def test_soft_constraint_is_accepted_when_no_swap_exists() -> None:
    roster = [
        RosterEntry(address="sccpt1aaaa", funder="same"),
        RosterEntry(address="sccpt1bbbb", funder="same"),
    ]
    matchups = pair(
        roster,
        BEACON,
        network="testnet",
        epoch=3,
        round_number=0,
        rounds_per_epoch=3,
    )
    assert len(matchups) == 1


def test_match_id_and_colours_are_recomputable() -> None:
    seats = ("sccpt1aaaa", "sccpt1bbbb")
    first = match_id("testnet", 3, 0, seats)
    assert first == match_id("testnet", 3, 0, list(reversed(seats)))
    assert first != match_id("testnet", 3, 1, seats)
    white = white_seat(BEACON, first, seats)
    assert white in seats
    assert white_seat(BEACON, first, list(reversed(seats))) == white
    with pytest.raises(PairingError):
        match_id("testnet", 3, 0, ["sccpt1aaaa", "sccpt1aaaa"])
    with pytest.raises(PairingError):
        match_id("testnet", 3, 0, ["sccpt1aaaa"])
    with pytest.raises(PairingError):
        white_seat(b"short", first, seats)


def test_pairing_rejects_bad_inputs() -> None:
    roster = _roster(2)
    with pytest.raises(PairingError, match="beacon"):
        pair(
            roster,
            b"short",
            network="testnet",
            epoch=3,
            round_number=0,
            rounds_per_epoch=3,
        )
    with pytest.raises(PairingError, match="duplicate"):
        pair(
            [roster[0], roster[0]],
            BEACON,
            network="testnet",
            epoch=3,
            round_number=0,
            rounds_per_epoch=3,
        )
    with pytest.raises(PairingError, match="rounds_per_epoch"):
        pair(
            roster,
            BEACON,
            network="testnet",
            epoch=3,
            round_number=0,
            rounds_per_epoch=0,
        )


def test_verify_pairing_accepts_exact_match_and_rejects_tampering() -> None:
    roster = _roster(4)
    matchups = pair(
        roster,
        BEACON,
        network="testnet",
        epoch=3,
        round_number=0,
        rounds_per_epoch=3,
    )
    matchup = matchups[0]
    assert verify_pairing(matchup, roster, BEACON, rounds_per_epoch=3)
    assert not verify_pairing(
        matchup.model_copy(update={"white": matchup.black, "black": matchup.white}),
        roster,
        BEACON,
        rounds_per_epoch=3,
    )
    assert not verify_pairing(
        matchup.model_copy(update={"round": 1}),
        roster,
        BEACON,
        rounds_per_epoch=3,
    )


def test_pairing_score_binds_network_beacon_round_and_address() -> None:
    base = pairing_score("testnet", BEACON, 0, "sccpt1aaaa")
    assert base != pairing_score("mainnet", BEACON, 0, "sccpt1aaaa")
    assert base != pairing_score("testnet", OTHER_BEACON, 0, "sccpt1aaaa")
    assert base != pairing_score("testnet", BEACON, 1, "sccpt1aaaa")
    assert base != pairing_score("testnet", BEACON, 0, "sccpt1bbbb")
