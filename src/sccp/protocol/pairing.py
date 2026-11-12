"""Deterministic pairing from a roster and beacon (section 5.6).

``pair`` is a pure function: every node that sees the same roster, beacon,
epoch and round emits the same matchup list, so the relay only delivers a result
that anyone can recompute. Scores are byte concatenations rather than canonical
JSON because they are internal ordering keys, never a signed payload:

``score(m) = SHA256("sccp/v1/pair" || network || beacon || u64(round) || address)``

Hard constraints (distinct addresses, quota remaining) are never violated. Soft
constraints (shared funder/subnet/ASN, recent opponents) are repaired by swapping
the second seat with a later candidate; if no swap fixes the pair the violation
is accepted, because keeping the epoch alive beats a diversity nicety.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from typing import Final

from sccp.errors import PairingError
from sccp.protocol.models import Matchup
from sccp.serialization import Domain, domain_hex

PAIR_SCORE_DOMAIN: Final = Domain.PAIR.value.encode("utf-8")
"""Prefix of the internal pairing score, per section 5.6."""

COLOUR_TAG: Final = b"color"
"""Separator between the beacon and match id in the colour draw."""


@dataclass(frozen=True, slots=True)
class RosterEntry:
    """A matchable miner and the soft-constraint metadata known about it."""

    address: str
    max_games: int = 1
    funder: str | None = None
    subnet: str | None = None
    asn: int | None = None
    recent_opponents: tuple[str, ...] = ()


def _u64(value: int, name: str) -> bytes:
    if not isinstance(value, int) or isinstance(value, bool):
        raise PairingError(f"{name} must be an integer")
    if not 0 <= value < 2**64:
        raise PairingError(f"{name} must be in [0, 2**64 - 1], got {value}")
    return value.to_bytes(8, "big")


def _check_roster(roster: Sequence[RosterEntry]) -> None:
    seen: set[str] = set()
    for entry in roster:
        if not isinstance(entry, RosterEntry):
            raise PairingError(f"roster entries must be RosterEntry, got {type(entry).__name__}")
        if not entry.address:
            raise PairingError("roster entries need a non-empty address")
        if entry.address in seen:
            raise PairingError(f"duplicate roster address {entry.address}")
        seen.add(entry.address)
        if entry.max_games < 1:
            raise PairingError(f"{entry.address} must allow at least one game")


def pairing_score(network: str, beacon: bytes, round_number: int, address: str) -> bytes:
    """The 32-byte ordering key for one miner in one round."""
    return sha256(
        PAIR_SCORE_DOMAIN
        + network.encode("utf-8")
        + beacon
        + _u64(round_number, "round")
        + address.encode("utf-8")
    ).digest()


def match_id(network: str, epoch: int, round_number: int, seats: Sequence[str]) -> str:
    """``H("sccp/v1/match", {network, epoch, round, seats})`` as hex.

    The opening is deliberately absent, so the match id never depends on the
    opening hash it is later used to select.
    """
    if len(seats) != 2:
        raise PairingError(f"a match has exactly two seats, got {len(seats)}")
    seat_a, seat_b = sorted(seats)
    if seat_a == seat_b:
        raise PairingError("the two seats of a match must be distinct")
    return domain_hex(
        Domain.MATCH,
        {
            "network": network,
            "epoch": epoch,
            "round": round_number,
            "seats": [seat_a, seat_b],
        },
    )


def white_seat(beacon: bytes, match: str, seats: Sequence[str]) -> str:
    """``SHA256(beacon || "color" || match_id)[:8] & 1``: 0 means seat A is White."""
    if not isinstance(beacon, bytes):
        raise PairingError(f"beacon must be bytes, got {type(beacon).__name__}")
    if len(beacon) != 32:
        raise PairingError(f"beacon must be 32 bytes, got {len(beacon)}")
    if len(seats) != 2:
        raise PairingError(f"a match has exactly two seats, got {len(seats)}")
    seat_a, seat_b = sorted(seats)
    if seat_a == seat_b:
        raise PairingError("the two seats of a match must be distinct")
    digest = sha256(beacon + COLOUR_TAG + match.encode("utf-8")).digest()
    return seat_a if int.from_bytes(digest[:8], "big") & 1 == 0 else seat_b


def _default_quotas(
    roster: Sequence[RosterEntry],
    round_number: int,
    rounds_per_epoch: int,
) -> dict[str, int]:
    """One game per miner per round, so a quota of ``g`` allows ``g`` rounds."""
    return {
        entry.address: max(0, min(entry.max_games, rounds_per_epoch) - round_number)
        for entry in roster
    }


def _violates(a: RosterEntry, b: RosterEntry, opponent_cooldown: int) -> bool:
    """Whether pairing ``a`` with ``b`` breaks a soft diversity constraint."""
    if a.funder is not None and a.funder == b.funder:
        return True
    if a.subnet is not None and a.subnet == b.subnet:
        return True
    if a.asn is not None and a.asn == b.asn:
        return True
    recent = opponent_cooldown > 0 and (
        b.address in a.recent_opponents or a.address in b.recent_opponents
    )
    return recent


def _matchup(network: str, epoch: int, round_number: int, a: str, b: str, beacon: bytes) -> Matchup:
    seats = tuple(sorted((a, b)))
    match = match_id(network, epoch, round_number, seats)
    white = white_seat(beacon, match, seats)
    black = seats[0] if white == seats[1] else seats[1]
    return Matchup(
        network=network,
        match_id=match,
        epoch=epoch,
        round=round_number,
        white=white,
        black=black,
    )


def pair(
    roster: Sequence[RosterEntry],
    beacon: bytes,
    *,
    network: str,
    epoch: int,
    round_number: int,
    rounds_per_epoch: int,
    quotas: Mapping[str, int] | None = None,
    opponent_cooldown: int = 0,
) -> list[Matchup]:
    """Pair one round's candidates, deterministically and respecting quotas.

    ``quotas`` overrides the remaining games per address; by default a miner may
    play one game in each of its first ``min(max_games, rounds_per_epoch)``
    rounds. Miners with no quota left or an odd one out sit the round out.
    """
    if not isinstance(beacon, bytes):
        raise PairingError(f"beacon must be bytes, got {type(beacon).__name__}")
    if len(beacon) != 32:
        raise PairingError(f"beacon must be 32 bytes, got {len(beacon)}")
    if not isinstance(network, str) or not network:
        raise PairingError("network must be a non-empty string")
    _u64(epoch, "epoch")
    _u64(round_number, "round")
    if not isinstance(rounds_per_epoch, int) or isinstance(rounds_per_epoch, bool):
        raise PairingError("rounds_per_epoch must be an integer")
    if rounds_per_epoch < 1:
        raise PairingError("rounds_per_epoch must be at least 1")
    if not isinstance(opponent_cooldown, int) or isinstance(opponent_cooldown, bool):
        raise PairingError("opponent_cooldown must be an integer")
    if opponent_cooldown < 0:
        raise PairingError("opponent_cooldown must not be negative")
    _check_roster(roster)

    if quotas is None:
        remaining = _default_quotas(roster, round_number, rounds_per_epoch)
    else:
        known = {entry.address for entry in roster}
        unknown = sorted(set(quotas) - known)
        if unknown:
            raise PairingError(f"quotas name addresses outside the roster: {unknown}")
        remaining = {}
        for entry in roster:
            value = quotas.get(entry.address, 0)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise PairingError(f"quota for {entry.address} must be a non-negative integer")
            remaining[entry.address] = value

    candidates = [entry for entry in roster if remaining.get(entry.address, 0) > 0]
    candidates.sort(
        key=lambda entry: (
            pairing_score(network, beacon, round_number, entry.address),
            entry.address,
        )
    )

    matchups: list[Matchup] = []
    index = 0
    while index + 1 < len(candidates):
        first = candidates[index]
        second = candidates[index + 1]
        if _violates(first, second, opponent_cooldown):
            replacement = next(
                (
                    position
                    for position in range(index + 2, len(candidates))
                    if not _violates(first, candidates[position], opponent_cooldown)
                ),
                None,
            )
            if replacement is not None:
                candidates[index + 1], candidates[replacement] = (
                    candidates[replacement],
                    candidates[index + 1],
                )
                second = candidates[index + 1]
        matchups.append(
            _matchup(
                network,
                epoch,
                round_number,
                first.address,
                second.address,
                beacon,
            )
        )
        remaining[first.address] -= 1
        remaining[second.address] -= 1
        index += 2

    return matchups


def verify_pairing(
    matchup: Matchup,
    roster: Sequence[RosterEntry],
    beacon: bytes,
    *,
    rounds_per_epoch: int,
    quotas: Mapping[str, int] | None = None,
    opponent_cooldown: int = 0,
) -> bool:
    """Return whether ``matchup`` is exactly what :func:`pair` emits."""
    try:
        expected = pair(
            roster,
            beacon,
            network=matchup.network,
            epoch=matchup.epoch,
            round_number=matchup.round,
            rounds_per_epoch=rounds_per_epoch,
            quotas=quotas,
            opponent_cooldown=opponent_cooldown,
        )
    except PairingError:
        return False
    return any(candidate == matchup for candidate in expected)


__all__ = [
    "COLOUR_TAG",
    "PAIR_SCORE_DOMAIN",
    "RosterEntry",
    "match_id",
    "pair",
    "pairing_score",
    "verify_pairing",
    "white_seat",
]
