"""Gossip envelopes, the peer table and pull-sync (plan section 5.12).

Everything here is transport-agnostic on purpose. An envelope is a signed
pydantic document; the peer table is an in-memory abuse score; pull-sync walks
heights through a ``PeerClient`` protocol. The HTTP transport lives in
:mod:`sccp.node.app` (server) and :class:`sccp.node.service.HttpPeerClient`
(client), so gossip and sync are testable without sockets.

Design rules taken from the plan:

- The envelope is ``{network, kind, payload, sender, signature}``. The sender
  signs ``sha256(domain + 0x00 + canonical_json(...))`` in the
  ``sccp/v1/gossip`` domain, so an envelope cannot be replayed on another
  network or re-labelled as another kind.
- Objects are deduplicated by their *content* id (txid, proof id, block hash,
  peer URL), never by anything the sender chooses.
- Fanout goes to a random subset of eligible peers (default four) and never
  back to the sender.
- Failures raise the peer's abuse score; enough failures ban it for a while.
"""

from __future__ import annotations

import random
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from pydantic import Field

from sccp.chain.block import block_hash
from sccp.chain.tx import txid
from sccp.crypto.address import encode_address
from sccp.crypto.keys import Keypair, sign_domain, verify_domain
from sccp.errors import GossipError
from sccp.protocol.models import (
    AddressText,
    Block,
    ProtocolModel,
    SignatureHex,
    Transaction,
    WinProof,
)
from sccp.serialization import Domain

MAX_FANOUT = 4
"""Default number of peers an envelope is relayed to."""

MAX_PEER_FAILURES = 3
"""Consecutive failures after which a peer is banned."""

BAN_SECONDS = 300
"""How long a banned peer is ignored."""

MAX_SEEN_OBJECTS = 4096
"""Dedupe cache bound; the oldest entry is evicted first."""


class GossipKind(StrEnum):
    """What an envelope carries (plan section 5.12)."""

    TRANSACTION = "transaction"
    PROOF = "proof"
    BLOCK = "block"
    PEER = "peer"


class GossipEnvelope(ProtocolModel):
    """A signed gossip message from one identity."""

    network: str = Field(min_length=1, max_length=32)
    kind: GossipKind
    payload: dict[str, object]
    sender: AddressText
    signature: SignatureHex


def envelope_payload(
    network: str,
    kind: GossipKind | str,
    payload: Mapping[str, object],
    sender: str,
) -> dict[str, object]:
    """The canonical, signed body of an envelope."""
    return {
        "network": network,
        "kind": str(kind),
        "payload": dict(payload),
        "sender": sender,
    }


def sign_envelope(
    keypair: Keypair,
    *,
    network: str,
    kind: GossipKind | str,
    payload: Mapping[str, object],
) -> GossipEnvelope:
    """Sign ``payload`` as ``keypair``'s address on ``network``."""
    sender = encode_address(keypair.public_key_bytes, network)
    body = envelope_payload(network, kind, payload, sender)
    signature = sign_domain(keypair, Domain.GOSSIP, body).hex()
    return GossipEnvelope(
        network=network,
        kind=GossipKind(kind),
        payload=dict(payload),
        sender=sender,
        signature=signature,
    )


def verify_envelope(
    envelope: GossipEnvelope,
    *,
    network: str,
    public_key: str,
) -> None:
    """Check ``envelope``'s network id and signature; raise :class:`GossipError`."""
    if envelope.network != network:
        raise GossipError(f"envelope is for network {envelope.network!r}, this node is {network!r}")
    try:
        signature = bytes.fromhex(envelope.signature)
    except ValueError as exc:
        raise GossipError(f"envelope signature is not hex: {exc}") from exc
    body = envelope_payload(envelope.network, envelope.kind, envelope.payload, envelope.sender)
    try:
        key_bytes = bytes.fromhex(public_key)
        valid = verify_domain(key_bytes, Domain.GOSSIP, body, signature)
    except (ValueError, TypeError) as exc:
        raise GossipError(f"cannot verify envelope signature: {exc}") from exc
    if not valid:
        raise GossipError(f"envelope from {envelope.sender} has a bad signature")


def object_id_of(kind: GossipKind, payload: Mapping[str, object]) -> str:
    """The content id of a gossiped object.

    Ids are recomputed from the object itself, so a sender cannot make two
    different blocks look like one. Raises :class:`GossipError` when the
    payload is malformed or of the wrong kind.
    """
    try:
        if kind is GossipKind.TRANSACTION:
            item = Transaction.model_validate(payload.get("transaction"))
            return txid(item)
        if kind is GossipKind.PROOF:
            proof = WinProof.model_validate(payload.get("proof"))
            return proof.proof_id
        if kind is GossipKind.BLOCK:
            block = Block.model_validate(payload.get("block"))
            return block_hash(block.header)
        if kind is GossipKind.PEER:
            url = payload.get("url")
            if not isinstance(url, str) or not url:
                raise GossipError("a peer envelope needs a url")
            return url
    except ValueError as exc:
        raise GossipError(f"cannot compute the {kind} object id: {exc}") from exc
    raise GossipError(f"unknown gossip kind {kind!r}")  # pragma: no cover - enum is closed


@dataclass(slots=True)
class PeerRecord:
    """One known peer and its abuse score."""

    url: str
    address: str | None = None
    height: int = 0
    failures: int = 0
    banned_until: int = 0
    last_seen: int = 0


@dataclass(slots=True)
class PeerTable:
    """A bounded table of peers, with failure-based banning."""

    max_peers: int = 64
    records: dict[str, PeerRecord] = field(default_factory=dict)

    def __len__(self) -> int:
        """Number of known peers."""
        return len(self.records)

    def __contains__(self, url: object) -> bool:
        return isinstance(url, str) and url in self.records

    def get(self, url: str) -> PeerRecord | None:
        """The record for ``url``, if known."""
        return self.records.get(url)

    def add(
        self,
        url: str,
        *,
        address: str | None = None,
        height: int = 0,
        now: int = 0,
    ) -> PeerRecord:
        """Add (or refresh) a peer. Refuses to grow past ``max_peers``."""
        if not url:
            raise GossipError("a peer url cannot be empty")
        record = self.records.get(url)
        if record is None:
            if len(self.records) >= self.max_peers:
                raise GossipError(f"peer table is full ({self.max_peers} peers)")
            record = PeerRecord(url=url)
            self.records[url] = record
        if address is not None:
            record.address = address
        record.height = max(record.height, height)
        record.last_seen = max(record.last_seen, now)
        return record

    def remove(self, url: str) -> None:
        """Forget one peer."""
        self.records.pop(url, None)

    def record_success(self, url: str, *, height: int | None = None, now: int = 0) -> PeerRecord:
        """Clear accumulated failures for a responsive peer."""
        record = self.add(url, height=height or 0, now=now)
        record.failures = 0
        record.banned_until = 0
        if height is not None:
            record.height = height
        return record

    def record_failure(self, url: str, *, now: int) -> PeerRecord:
        """Count one failure, banning the peer once ``MAX_PEER_FAILURES`` is hit."""
        record = self.add(url)
        record.failures += 1
        if record.failures >= MAX_PEER_FAILURES:
            record.banned_until = max(record.banned_until, now + BAN_SECONDS)
        return record

    def is_banned(self, url: str, *, now: int) -> bool:
        """Whether ``url`` is currently ignored."""
        record = self.records.get(url)
        return record is not None and record.banned_until > now

    def eligible(self, *, now: int) -> list[PeerRecord]:
        """Every peer that is not banned, sorted by URL for determinism."""
        return [record for _, record in sorted(self.records.items()) if record.banned_until <= now]


@dataclass(slots=True)
class GossipCache:
    """A bounded, insertion-ordered set of object ids for deduplication."""

    max_entries: int = MAX_SEEN_OBJECTS
    seen: dict[tuple[str, str], int] = field(default_factory=dict)

    def __len__(self) -> int:
        """How many ids are remembered."""
        return len(self.seen)

    def check_and_add(self, kind: GossipKind | str, object_id: str, *, now: int = 0) -> bool:
        """Record ``object_id`` and return ``True`` only the first time it is seen."""
        key = (str(kind), object_id)
        if key in self.seen:
            self.seen[key] = now
            return False
        self.seen[key] = now
        while len(self.seen) > self.max_entries:
            oldest = next(iter(self.seen))
            del self.seen[oldest]
        return True


def choose_fanout(
    peers: Iterable[PeerRecord],
    *,
    rng: random.Random,
    count: int = MAX_FANOUT,
    exclude: Iterable[str] = (),
) -> tuple[str, ...]:
    """Pick up to ``count`` peers at random, never including ``exclude``."""
    blocked = set(exclude)
    candidates = sorted(record.url for record in peers if record.url not in blocked)
    if count >= len(candidates):
        return tuple(candidates)
    return tuple(rng.sample(candidates, count))


class PeerClient(Protocol):
    """What pull-sync needs from a peer's HTTP API."""

    def status(self) -> Mapping[str, object]:
        """The peer's ``/v1/status`` document."""

    def block(self, reference: str) -> Mapping[str, object]:
        """The peer's ``/v1/blocks/{reference}`` document."""


def plan_sync(local_height: int, remote_height: int, *, max_blocks: int = 512) -> tuple[int, int]:
    """The ``(start, end)`` heights to request, inclusive; empty when caught up."""
    start = local_height + 1
    if remote_height < start:
        return (start, start - 1)
    return (start, min(remote_height, start + max_blocks - 1))


def fetch_blocks(
    client: PeerClient,
    *,
    start_height: int,
    end_height: int,
) -> list[Block]:
    """Download blocks in ``[start_height, end_height]`` in order.

    A malformed response aborts the sync with :class:`GossipError`: a peer that
    cannot answer a block query is not trusted with a partial chain.
    """
    blocks: list[Block] = []
    for height in range(start_height, end_height + 1):
        document = client.block(str(height))
        try:
            block = Block.model_validate(document.get("block", document))
        except ValueError as exc:
            raise GossipError(f"peer returned a malformed block at height {height}: {exc}") from exc
        if block.header.height != height:
            raise GossipError(f"peer returned height {block.header.height} for {height}")
        blocks.append(block)
    return blocks


__all__ = [
    "BAN_SECONDS",
    "MAX_FANOUT",
    "MAX_PEER_FAILURES",
    "MAX_SEEN_OBJECTS",
    "GossipCache",
    "GossipEnvelope",
    "GossipKind",
    "PeerClient",
    "PeerRecord",
    "PeerTable",
    "choose_fanout",
    "envelope_payload",
    "fetch_blocks",
    "object_id_of",
    "plan_sync",
    "sign_envelope",
    "verify_envelope",
]
