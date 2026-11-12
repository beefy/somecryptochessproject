"""Follow the chain's epoch so the relay matches the roster the chain chose.

A relay config pins one epoch: its beacon, its roster and the per-miner game
quotas. That is enough to start, but the chain moves on. Availability is
committed ``availability_lead`` epochs ahead and revealed one epoch ahead, so
the roster for epoch ``E`` is fixed by ``E``'s first block; a relay that keeps
playing the epoch it was configured with therefore plays matches whose proofs
the chain no longer accepts.

This module closes that gap: it polls the node, and as soon as the chain has
opened a later, matchable epoch it hands the relay that epoch's beacon and
roster (:meth:`~sccp.miner.matchmaker.Matchmaker.advance_epoch`). Everything the
relay decides -- pairings, quotas, round numbers -- then follows from chain
state rather than from the config file.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from sccp.chain.state import epoch_of
from sccp.config import NetworkParams, get_network
from sccp.errors import RelayError
from sccp.miner.matchmaker import Matchmaker
from sccp.protocol.models import Beacon, RosterMember
from sccp.protocol.pairing import RosterEntry

MINIMUM_MINERS = 2
"""A round needs two seats, so an epoch with fewer matchable miners is skipped."""


class EpochNode(Protocol):
    """The node calls the follower needs: tip height and one epoch's roster."""

    def status(self) -> Mapping[str, object]:
        """The node's ``/v1/status`` document."""

    def epoch(self, epoch: int) -> Mapping[str, object]:
        """The node's ``/v1/epochs/{epoch}`` document."""


@dataclass(frozen=True, slots=True)
class EpochAdvance:
    """One successful move onto a later epoch."""

    epoch: int
    miners: int

    def describe(self) -> str:
        """A one-line summary for the relay's log."""
        return f"epoch {self.epoch} with {self.miners} matchable miner(s)"


@dataclass(slots=True)
class EpochFollower:
    """Keep a relay's beacon and roster in step with the chain."""

    params: NetworkParams
    matchmaker: Matchmaker
    node: EpochNode

    def __post_init__(self) -> None:
        self.params = get_network(self.params)

    @property
    def epoch(self) -> int:
        """The epoch the relay is currently matching for."""
        return self.matchmaker.beacon.epoch

    def step(self) -> EpochAdvance | None:
        """Advance the relay to the chain's epoch, when it is matchable.

        Returns ``None`` while the chain is on the relay's own epoch, has not
        opened the next one yet, or has too few matchable miners to pair. A node
        that cannot be reached raises :class:`~sccp.errors.RelayError`, which the
        caller turns into a log line and retries on the next poll.
        """
        status = self.node.status()
        height = _int_field(status, "height", where="/v1/status")
        target = epoch_of(height, self.params)
        if target <= self.epoch:
            return None
        try:
            view = self.node.epoch(target)
        except RelayError:
            # The chain has not opened the epoch yet: the next block will.
            return None
        return self.apply(target, view)

    def apply(self, epoch: int, view: Mapping[str, object]) -> EpochAdvance | None:
        """Advance the relay using one ``/v1/epochs/{epoch}`` document."""
        reported = view.get("epoch")
        if reported != epoch:
            raise RelayError(f"node returned epoch {reported!r} for epoch {epoch}")
        if view.get("matchable") is not True:
            return None
        beacon_document = view.get("beacon")
        if not isinstance(beacon_document, Mapping):
            return None
        try:
            beacon = Beacon.model_validate(beacon_document)
        except ValueError as exc:
            raise RelayError(f"epoch {epoch} beacon is invalid: {exc}") from None
        if beacon.epoch != epoch:
            raise RelayError(f"epoch {epoch} carries a beacon for epoch {beacon.epoch}")

        entries, public_keys, engines = _roster_entries(epoch, view)
        if len(entries) < MINIMUM_MINERS:
            return None
        advanced = self.matchmaker.advance_epoch(
            beacon,
            roster=entries,
            public_keys=public_keys,
            engines=engines,
        )
        return EpochAdvance(epoch=epoch, miners=len(entries)) if advanced else None


def _roster_entries(
    epoch: int, view: Mapping[str, object]
) -> tuple[dict[str, RosterEntry], dict[str, str], dict[str, str]]:
    """Split an epoch view into the roster, its keys and its engine fingerprints."""
    document = view.get("roster")
    if not isinstance(document, Mapping):
        raise RelayError(f"epoch {epoch} has no roster object")
    quotas = view.get("quotas")
    quota_by_miner = quotas if isinstance(quotas, Mapping) else {}
    entries: dict[str, RosterEntry] = {}
    public_keys: dict[str, str] = {}
    engines: dict[str, str] = {}
    for address, raw in document.items():
        if not isinstance(address, str) or not isinstance(raw, Mapping):
            raise RelayError(f"epoch {epoch} roster entries must map an address to an object")
        try:
            member = RosterMember.model_validate(raw)
        except ValueError as exc:
            raise RelayError(f"epoch {epoch} roster entry {address} is invalid: {exc}") from None
        if member.engine is None:
            continue
        quota = quota_by_miner.get(address, member.max_games)
        if not isinstance(quota, int) or isinstance(quota, bool) or quota < 1:
            continue
        entries[address] = RosterEntry(
            address=address,
            max_games=quota,
            funder=member.funder,
            subnet=member.subnet,
            asn=member.asn,
            recent_opponents=tuple(member.recent_opponents),
        )
        public_keys[address] = member.public_key
        engines[address] = member.engine
    return entries, public_keys, engines


def _int_field(document: Mapping[str, object], key: str, *, where: str) -> int:
    """Read a non-negative integer field, or raise."""
    value = document.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise RelayError(f"{where} did not return a usable {key!r}: {value!r}")
    return value


__all__ = ["MINIMUM_MINERS", "EpochAdvance", "EpochFollower", "EpochNode"]
