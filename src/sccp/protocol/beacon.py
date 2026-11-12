"""Epoch beacons: commit/reveal derivation and optional drand mixing (section 5.5).

The beacon is a pure function of public data::

    digest = SHA256("sccp/v1/beacon" || 0x00 || network || 0x00 || u64(epoch)
                    || block_hash || concat(sorted(commitment || nonce)))
    B_e    = SHA256(digest || drand_signature)      when drand is used
    B_e    = digest                                  otherwise

Each entry is the revealed commitment's 32 bytes followed by the 32-byte nonce;
entries are sorted as byte strings, so reveal order never matters.

drand quicknet signs with BLS12-381, which ``cryptography`` does not implement.
Rather than pull in a second crypto stack for M2, signature checking is an
injected :class:`DrandVerifier`: a caller that wants drand supplies one (M4 wires
the pinned quicknet key). ``drand_used = 0`` is always valid and needs no
verifier.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from hashlib import sha256
from typing import Final, Protocol

from sccp.crypto.address import validate_address
from sccp.errors import BeaconError
from sccp.protocol.models import (
    Availability,
    Beacon,
    BeaconReveal,
    Reveal,
)
from sccp.serialization import Domain, domain_hex

BEACON_BYTES: Final = 32
"""Beacon digests and nonces are 32 bytes."""

U64_MAX: Final = 2**64 - 1

DRAND_QUICKNET_PERIOD_SECONDS: Final = 3
"""quicknet publishes a round every 3 seconds (section 5.5)."""

DRAND_QUICKNET_ROUND_OFFSET: Final = 1
"""``round(e) = mtp // period + offset`` for quicknet."""

MTP_WINDOW: Final = 11
"""The median time past is taken over the last 11 block timestamps."""

DRAND_SIGNATURE_BYTES: Final = 48
"""quicknet publishes 48-byte BLS12-381 signatures."""


class DrandVerifier(Protocol):
    """Verifies a drand signature for ``(round, signature)``.

    Implementations are responsible for checking the signature against the
    pinned chain key; :func:`verify_beacon` only enforces that a verifier exists
    and accepts the pair when ``drand_used = 1``.
    """

    def __call__(self, round_number: int, signature: str) -> bool: ...


def _check_u64(value: int, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise BeaconError(f"{name} must be an integer")
    if not 0 <= value <= U64_MAX:
        raise BeaconError(f"{name} must be in [0, 2**64 - 1], got {value}")
    return value


def _check_network(network: str) -> str:
    if not isinstance(network, str) or not network:
        raise BeaconError("network must be a non-empty string")
    if len(network) > 32 or not network.isascii() or "\x00" in network:
        raise BeaconError(f"invalid network id {network!r}")
    return network


def _hex_digest(value: str, name: str) -> bytes:
    if not isinstance(value, str):
        raise BeaconError(f"{name} must be a hex string")
    try:
        raw = bytes.fromhex(value)
    except ValueError:
        raise BeaconError(f"{name} is not valid hex") from None
    if len(raw) != BEACON_BYTES:
        raise BeaconError(f"{name} must be {BEACON_BYTES} bytes, got {len(raw)}")
    return raw


def _signature_bytes(value: str) -> bytes:
    """quicknet drand signatures are 48-byte BLS12-381 points."""
    if not isinstance(value, str):
        raise BeaconError("drand_signature must be a hex string")
    try:
        raw = bytes.fromhex(value)
    except ValueError:
        raise BeaconError("drand_signature is not valid hex") from None
    if len(raw) != DRAND_SIGNATURE_BYTES:
        raise BeaconError(f"drand_signature must be {DRAND_SIGNATURE_BYTES} bytes, got {len(raw)}")
    return raw


def commitment(address: str, epoch: int, nonce: str) -> str:
    """``H("sccp/v1/beacon-commit", {address, epoch, nonce})`` as hex.

    The payload deliberately omits the network: an address already commits to
    its network, so a testnet commitment can never be replayed on mainnet.
    """
    if not isinstance(address, str) or not address:
        raise BeaconError("address must be a non-empty string")
    _check_u64(epoch, "epoch")
    _hex_digest(nonce, "nonce")
    return domain_hex(
        Domain.BEACON_COMMIT,
        {"address": address, "epoch": epoch, "nonce": nonce},
    )


def reveal_for(availability: Availability, nonce: str) -> Reveal:
    """Build a reveal and check it opens ``availability.commitment``."""
    if commitment(availability.miner, availability.epoch, nonce) != availability.commitment:
        raise BeaconError(
            f"nonce does not open the availability commitment for {availability.miner}"
        )
    return Reveal(
        network=availability.network,
        miner=availability.miner,
        epoch=availability.epoch,
        nonce=nonce,
    )


def validate_reveal(reveal: Reveal, availability: Availability) -> BeaconReveal:
    """Check a reveal against its availability tx and return the beacon entry."""
    if reveal.network != availability.network:
        raise BeaconError(
            f"reveal network {reveal.network!r} does not match availability "
            f"network {availability.network!r}"
        )
    if reveal.miner != availability.miner:
        raise BeaconError("reveal miner does not match its availability")
    if reveal.epoch != availability.epoch:
        raise BeaconError(
            f"reveal epoch {reveal.epoch} does not match availability epoch {availability.epoch}"
        )
    if commitment(reveal.miner, reveal.epoch, reveal.nonce) != availability.commitment:
        raise BeaconError(
            f"reveal nonce for {reveal.miner} does not open commitment {availability.commitment}"
        )
    return BeaconReveal(
        miner=reveal.miner,
        commitment=availability.commitment,
        nonce=reveal.nonce,
    )


def epoch_reveals(
    availability: Mapping[str, Availability],
    reveals: Sequence[Reveal],
    *,
    network: str,
    epoch: int,
) -> tuple[tuple[BeaconReveal, ...], tuple[str, ...]]:
    """Validate one epoch's reveals and report who withheld.

    Returns ``(entries, withheld)``. ``entries`` is the sorted beacon input;
    ``withheld`` lists availability miners with no valid reveal, sorted. A miner
    that withholds is excluded from the roster, but the fact is public and can be
    recorded by the chain (M3) or the relay's reputation endpoint.
    """
    network = _check_network(network)
    _check_u64(epoch, "epoch")
    by_miner: dict[str, Availability] = {}
    for key, committed in availability.items():
        if key != committed.miner:
            raise BeaconError(f"availability key {key!r} does not match miner {committed.miner!r}")
        if committed.network != network or committed.epoch != epoch:
            raise BeaconError(
                f"availability for {committed.miner} is for "
                f"{committed.network} epoch {committed.epoch}, "
                f"expected {network} epoch {epoch}"
            )
        if committed.miner in by_miner:
            raise BeaconError(f"duplicate availability for {committed.miner}")
        by_miner[committed.miner] = committed

    entries: list[BeaconReveal] = []
    seen: set[str] = set()
    for reveal in reveals:
        if reveal.miner in seen:
            raise BeaconError(f"duplicate reveal for {reveal.miner}")
        seen.add(reveal.miner)
        item = by_miner.get(reveal.miner)
        if item is None:
            raise BeaconError(f"reveal for {reveal.miner} has no availability tx")
        entries.append(validate_reveal(reveal, item))
    entries.sort(key=lambda item: (item.commitment, item.nonce, item.miner))
    withheld = tuple(sorted(set(by_miner) - seen))
    return tuple(entries), withheld


def beacon_entries(reveals: Sequence[BeaconReveal]) -> list[bytes]:
    """The sorted ``commitment || nonce`` entries of a reveal set."""
    entries: list[bytes] = []
    for reveal in reveals:
        entry = _hex_digest(reveal.commitment, "commitment") + _hex_digest(reveal.nonce, "nonce")
        entries.append(entry)
    entries.sort()
    return entries


def beacon_digest(
    network: str,
    epoch: int,
    block_hash: str,
    reveals: Sequence[BeaconReveal],
) -> bytes:
    """The pre-drand beacon digest (32 bytes)."""
    network = _check_network(network)
    _check_u64(epoch, "epoch")
    block = _hex_digest(block_hash, "block_hash")
    payload = (
        Domain.BEACON.value.encode("utf-8")
        + b"\x00"
        + network.encode("utf-8")
        + b"\x00"
        + epoch.to_bytes(8, "big")
        + block
        + b"".join(beacon_entries(reveals))
    )
    return sha256(payload).digest()


def beacon_hash(
    network: str,
    epoch: int,
    block_hash: str,
    reveals: Sequence[BeaconReveal],
    *,
    drand_signature: str | None = None,
) -> str:
    """``B_e`` as lowercase hex, mixed with drand when a signature is supplied."""
    digest = beacon_digest(network, epoch, block_hash, reveals)
    if drand_signature is not None:
        digest = sha256(digest + _signature_bytes(drand_signature)).digest()
    return digest.hex()


def compute_beacon(
    *,
    network: str,
    epoch: int,
    block_hash: str,
    reveals: Sequence[BeaconReveal],
    drand_round: int | None = None,
    drand_signature: str | None = None,
) -> Beacon:
    """Build a :class:`Beacon` model from its public inputs."""
    ordered = tuple(sorted(reveals, key=lambda item: (item.commitment, item.nonce, item.miner)))
    return Beacon(
        network=network,
        epoch=epoch,
        hash=beacon_hash(
            network,
            epoch,
            block_hash,
            ordered,
            drand_signature=drand_signature,
        ),
        block_hash=block_hash,
        reveals=ordered,
        drand_used=drand_signature is not None,
        drand_round=drand_round if drand_signature is not None else None,
        drand_signature=drand_signature,
    )


def verify_beacon(
    beacon: Beacon,
    *,
    block_hash: str | None = None,
    availability: dict[str, str] | None = None,
    drand_verifier: DrandVerifier | None = None,
    expected_round: int | None = None,
) -> None:
    """Recompute ``beacon.hash`` from public data or raise :class:`BeaconError`.

    ``availability`` maps a miner address to the commitment from its availability
    transaction; when supplied, every reveal must match it. ``expected_round``
    is usually :func:`derive_drand_round` of the epoch's MTP.
    """
    _check_network(beacon.network)
    _check_u64(beacon.epoch, "epoch")
    _hex_digest(beacon.hash, "beacon.hash")
    _hex_digest(beacon.block_hash, "beacon.block_hash")
    if block_hash is not None and beacon.block_hash != block_hash:
        raise BeaconError(f"beacon block hash is {beacon.block_hash}, expected {block_hash}")

    seen: set[str] = set()
    ordered: list[BeaconReveal] = []
    for reveal in beacon.reveals:
        if reveal.miner in seen:
            raise BeaconError(f"duplicate reveal for {reveal.miner}")
        seen.add(reveal.miner)
        if not validate_address(reveal.miner, beacon.network):
            raise BeaconError(f"reveal miner {reveal.miner!r} is not a {beacon.network} address")
        if commitment(reveal.miner, beacon.epoch, reveal.nonce) != reveal.commitment:
            raise BeaconError(f"reveal for {reveal.miner} does not open its commitment")
        if availability is not None:
            committed = availability.get(reveal.miner)
            if committed is None:
                raise BeaconError(f"reveal for {reveal.miner} has no availability tx")
            if committed != reveal.commitment:
                raise BeaconError(
                    f"reveal commitment for {reveal.miner} does not match its availability tx"
                )
        ordered.append(reveal)

    if beacon.drand_used:
        if beacon.drand_signature is None:
            raise BeaconError("drand_used is set but the beacon has no signature")
        if beacon.drand_round is None:
            raise BeaconError("drand_used is set but the beacon has no round")
        if expected_round is not None and beacon.drand_round != expected_round:
            raise BeaconError(f"drand round is {beacon.drand_round}, expected {expected_round}")
        if drand_verifier is None:
            raise BeaconError("drand signature cannot be verified without a pinned verifier")
        if not drand_verifier(beacon.drand_round, beacon.drand_signature):
            raise BeaconError("drand signature does not verify against the pinned chain key")
    else:
        if beacon.drand_signature is not None:
            raise BeaconError("beacon carries a drand signature but drand_used is false")
        if beacon.drand_round is not None:
            raise BeaconError("beacon carries a drand round but drand_used is false")

    recomputed = beacon_hash(
        beacon.network,
        beacon.epoch,
        beacon.block_hash,
        ordered,
        drand_signature=beacon.drand_signature,
    )
    if recomputed != beacon.hash:
        raise BeaconError(
            f"beacon hash is {beacon.hash}, but the public data recomputes to {recomputed}"
        )


def median_time_past(timestamps: Sequence[int]) -> int:
    """Median of the last :data:`MTP_WINDOW` timestamps.

    For an even-length input the lower median is used, so the result is always
    one of the supplied integers and therefore platform-independent.
    """
    if not timestamps:
        raise BeaconError("median time past needs at least one timestamp")
    window: list[int] = []
    for value in timestamps[-MTP_WINDOW:]:
        if not isinstance(value, int) or isinstance(value, bool):
            raise BeaconError("block timestamps must be integers")
        if value < 0:
            raise BeaconError("block timestamps must not be negative")
        window.append(value)
    window.sort()
    return window[(len(window) - 1) // 2]


def derive_drand_round(
    mtp: int,
    *,
    period_seconds: int = DRAND_QUICKNET_PERIOD_SECONDS,
    offset: int = DRAND_QUICKNET_ROUND_OFFSET,
) -> int:
    """``round(e) = mtp // period_seconds + offset`` (section 5.5, step 6)."""
    if not isinstance(mtp, int) or isinstance(mtp, bool) or mtp < 0:
        raise BeaconError("median time past must be a non-negative integer")
    if not isinstance(period_seconds, int) or isinstance(period_seconds, bool):
        raise BeaconError("drand period must be an integer")
    if period_seconds <= 0:
        raise BeaconError("drand period must be positive")
    if not isinstance(offset, int) or isinstance(offset, bool):
        raise BeaconError("drand round offset must be an integer")
    round_number = mtp // period_seconds + offset
    return _check_u64(round_number, "drand round")


__all__ = [
    "BEACON_BYTES",
    "DRAND_QUICKNET_PERIOD_SECONDS",
    "DRAND_QUICKNET_ROUND_OFFSET",
    "DRAND_SIGNATURE_BYTES",
    "MTP_WINDOW",
    "DrandVerifier",
    "beacon_digest",
    "beacon_entries",
    "beacon_hash",
    "commitment",
    "compute_beacon",
    "derive_drand_round",
    "epoch_reveals",
    "median_time_past",
    "reveal_for",
    "validate_reveal",
    "verify_beacon",
]
