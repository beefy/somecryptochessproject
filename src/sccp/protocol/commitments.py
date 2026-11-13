"""Folded commitments over the high-volume streams (PROTOCOL.md section 8.5).

Live play signs every ply with Ed25519: one signature per move keeps the relay
cheap and small, and a chess move is not worth 3.3 KB. The *claim*, though, is
never allowed to rest on Ed25519 alone. Both sides instead fold the whole stream
into a 32-byte root and hybrid-sign that root once: ``move_commitment`` inside a
game result, and a :class:`~sccp.protocol.models.StreamCheckpoint` inside
evidence. A quantum attacker who forges per-ply Ed25519 signatures still cannot
produce a root covered by an honest hybrid signature.

The fold is a single SHA-256 chain, deliberately dumb so a browser can compute
it with the same ``sha256`` helper it already uses::

    root_0 = 0x00 * 32
    root_{i+1} = sha256("sccp/v1/commitment" || 0x00 || root_i
                        || canonical_json(leaf_i))

Each leaf is canonical JSON with sorted keys and no whitespace, exactly like
every other hash in the protocol, and the domain string goes in front so the
fold can never collide with a transaction, block or game digest.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from hashlib import sha256
from typing import Final

from sccp.protocol.models import (
    Color,
    CommittedMove,
    FlagReceipt,
    MatchCertificate,
    MoveReceipt,
    StreamCheckpoint,
)
from sccp.serialization import Domain, canonical_json

ZERO_ROOT: Final = "00" * 32
"""The fold's starting value, as lowercase hex."""

_FOLD_PREFIX: Final = Domain.COMMITMENT.encode("utf-8") + b"\x00"


def fold(leaves: Iterable[Mapping[str, object]]) -> str:
    """Fold canonical-JSON leaves into a 32-byte root, returned as hex."""
    root = bytes.fromhex(ZERO_ROOT)
    for leaf in leaves:
        root = sha256(_FOLD_PREFIX + root + canonical_json(leaf)).digest()
    return root.hex()


def move_leaf(network: str, match_id: str, seat: str, ply: int, san: str) -> dict[str, object]:
    """The committed form of one ply.

    ``ply`` is the zero-based SAN index in the full game, so the leaf of a given
    move is identical whoever computes it: a seat building a result, a relay
    check-pointing the stream it relayed, or a verifier replaying evidence.
    """
    return {"match_id": match_id, "network": network, "ply": ply, "san": san, "seat": seat}


def seat_for_ply(ply: int) -> Color:
    """The colour that moves at zero-based SAN index ``ply``."""
    return "white" if ply % 2 == 0 else "black"


def move_root(
    network: str,
    match_id: str,
    seats: Mapping[Color, str],
    san_moves: Sequence[str],
    *,
    start_ply: int = 0,
    end_ply: int | None = None,
) -> str:
    """The committed root of ``san_moves[start_ply:end_ply]``.

    ``seats`` maps ``"white"``/``"black"`` to the certificate's addresses. The
    default range is the whole game; a caller that only wants the post-opening
    stream (the relay, whose receipts start after the book line) passes
    ``start_ply = len(certificate.opening.moves)``.
    """
    stop = len(san_moves) if end_ply is None else end_ply
    if not 0 <= start_ply <= stop <= len(san_moves):
        raise ValueError("move_root range is outside the SAN list")
    leaves = (
        move_leaf(network, match_id, seats[seat_for_ply(ply)], ply, san_moves[ply])
        for ply in range(start_ply, stop)
    )
    return fold(leaves)


def evidence_root(network: str, match_id: str, moves: Sequence[CommittedMove]) -> str:
    """The root of a :class:`~sccp.protocol.models.MoveEvidence` bundle."""
    return fold(move_leaf(network, match_id, move.seat, move.ply, move.san) for move in moves)


def certificate_move_root(certificate: MatchCertificate, san_moves: Sequence[str]) -> str:
    """The ``move_commitment`` both seats hybrid-sign inside a game result.

    It covers the post-opening plies only, exactly the stream the relay
    observes and receipts, so a result's commitment can be checked against a
    relay checkpoint byte for byte.
    """
    return move_root(
        certificate.network,
        certificate.match_id,
        {"white": certificate.white, "black": certificate.black},
        san_moves,
        start_ply=len(certificate.opening.moves),
    )


def receipt_leaf(network: str, receipt: MoveReceipt | FlagReceipt) -> dict[str, object]:
    """The exact payload a witness signs for a move or flag receipt.

    This is also the commitment leaf, so a receipt cannot be signed one way and
    folded another.
    """
    payload: dict[str, object] = {
        "black_ms": receipt.black_ms,
        "kind": receipt.kind,
        "match_id": receipt.match_id,
        "network": network,
        "ply": receipt.ply,
        "received_at_ms": receipt.received_at_ms,
        "seat": receipt.seat,
        "white_ms": receipt.white_ms,
    }
    if isinstance(receipt, MoveReceipt):
        payload["san"] = receipt.san
    return payload


def clock_root(
    network: str,
    receipts: Sequence[MoveReceipt],
    final: FlagReceipt,
) -> str:
    """The root of a clock-evidence window: the move receipts plus the flag."""
    return fold(
        [receipt_leaf(network, receipt) for receipt in receipts] + [receipt_leaf(network, final)]
    )


def checkpoint_payload(checkpoint: StreamCheckpoint) -> dict[str, object]:
    """The payload a witness hybrid-signs to attest a stream root."""
    return {
        "kind": checkpoint.kind,
        "match_id": checkpoint.match_id,
        "network": checkpoint.network,
        "ply": checkpoint.ply,
        "root": checkpoint.root,
    }


__all__ = [
    "ZERO_ROOT",
    "checkpoint_payload",
    "clock_root",
    "evidence_root",
    "fold",
    "move_leaf",
    "move_root",
    "receipt_leaf",
    "seat_for_ply",
]
