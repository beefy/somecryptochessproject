"""The folded stream commitments of PROTOCOL.md section 8.5.

The fold is the one construction that is not `H(domain, payload)`, so it is pinned
byte for byte here: the empty root, canonical leaf encoding, order sensitivity,
and the two roots that other modules consume (`certificate_move_root` for a
result, `clock_root` for a flag bundle).
"""

from __future__ import annotations

from hashlib import sha256

import pytest

from sccp.config import CLOCK_EVIDENCE_PLIES
from sccp.crypto.pq import hybrid_keypair_from_seed
from sccp.protocol.clocks import FIXED_TIME_CONTROL, build_clock_evidence
from sccp.protocol.commitments import (
    ZERO_ROOT,
    certificate_move_root,
    checkpoint_payload,
    clock_root,
    evidence_root,
    fold,
    move_leaf,
    move_root,
    receipt_leaf,
    seat_for_ply,
)
from sccp.protocol.models import (
    CommittedMove,
    FlagReceipt,
    MatchCertificate,
    MoveReceipt,
    OpeningRef,
    StreamCheckpoint,
    TimeControl,
)
from sccp.serialization import canonical_json

NETWORK = "testnet"
MATCH_ID = "ab" * 32
WHITE = "sccpt1white"
BLACK = "sccpt1black"


def test_the_empty_fold_is_a_zero_root() -> None:
    assert ZERO_ROOT == "00" * 32
    assert fold([]) == ZERO_ROOT


def test_the_fold_is_the_documented_chain() -> None:
    """Recompute the first two steps by hand from the prose."""
    leaf = move_leaf(NETWORK, MATCH_ID, WHITE, 0, "e4")
    expected = sha256(
        b"sccp/v1/commitment\x00" + bytes.fromhex(ZERO_ROOT) + canonical_json(leaf)
    ).hexdigest()
    assert fold([leaf]) == expected

    second = move_leaf(NETWORK, MATCH_ID, BLACK, 1, "e5")
    expected = sha256(
        b"sccp/v1/commitment\x00" + bytes.fromhex(expected) + canonical_json(second)
    ).hexdigest()
    assert fold([leaf, second]) == expected


def test_leaves_are_canonical_and_order_sensitive() -> None:
    first = move_leaf(NETWORK, MATCH_ID, WHITE, 0, "e4")
    second = move_leaf(NETWORK, MATCH_ID, BLACK, 1, "e5")
    assert fold([first, second]) != fold([second, first])
    assert fold([first, second]) != fold([first])


def test_seat_follows_the_ply() -> None:
    assert seat_for_ply(0) == "white"
    assert seat_for_ply(1) == "black"
    assert seat_for_ply(2) == "white"


def test_a_move_root_covers_the_requested_range_only() -> None:
    san_moves = ["e4", "e5", "Nf3", "Nc6"]
    seats = {"white": WHITE, "black": BLACK}
    whole = move_root(NETWORK, MATCH_ID, seats, san_moves)
    assert whole == fold(
        [
            move_leaf(NETWORK, MATCH_ID, seats[seat_for_ply(ply)], ply, san)
            for ply, san in enumerate(san_moves)
        ]
    )
    opening = move_root(NETWORK, MATCH_ID, seats, san_moves, start_ply=2)
    assert opening != whole
    with pytest.raises(ValueError):
        move_root(NETWORK, MATCH_ID, seats, san_moves, start_ply=9)
    with pytest.raises(ValueError):
        move_root(NETWORK, MATCH_ID, seats, san_moves, start_ply=3, end_ply=2)


def test_evidence_root_matches_a_plain_fold() -> None:
    moves = (
        CommittedMove(ply=4, seat=WHITE, san="Qh5"),
        CommittedMove(ply=5, seat=BLACK, san="Nf6"),
    )
    assert evidence_root(NETWORK, MATCH_ID, moves) == fold(
        [move_leaf(NETWORK, MATCH_ID, move.seat, move.ply, move.san) for move in moves]
    )


def test_receipt_leaves_are_the_signed_payload() -> None:
    receipt = MoveReceipt(
        match_id=MATCH_ID,
        ply=3,
        seat=BLACK,
        san="e5",
        received_at_ms=1_000,
        white_ms=1_799_000,
        black_ms=1_799_000,
    )
    assert receipt_leaf(NETWORK, receipt) == {
        "black_ms": 1_799_000,
        "kind": "move",
        "match_id": MATCH_ID,
        "network": NETWORK,
        "ply": 3,
        "received_at_ms": 1_000,
        "san": "e5",
        "seat": BLACK,
        "white_ms": 1_799_000,
    }
    flag = FlagReceipt(
        match_id=MATCH_ID,
        ply=4,
        seat=WHITE,
        received_at_ms=2_000,
        white_ms=0,
        black_ms=1_799_000,
    )
    assert "san" not in receipt_leaf(NETWORK, flag)


def test_clock_root_covers_the_window_then_the_flag() -> None:
    receipt = MoveReceipt(
        match_id=MATCH_ID,
        ply=0,
        seat=WHITE,
        san="e4",
        received_at_ms=1_000,
        white_ms=1_799_000,
        black_ms=1_800_000,
    )
    flag = FlagReceipt(
        match_id=MATCH_ID,
        ply=1,
        seat=BLACK,
        received_at_ms=2_000,
        white_ms=1_799_000,
        black_ms=0,
    )
    assert clock_root(NETWORK, (receipt,), flag) == fold(
        [receipt_leaf(NETWORK, receipt), receipt_leaf(NETWORK, flag)]
    )


def test_clock_evidence_takes_exactly_the_window() -> None:
    """A bundle is the last `CLOCK_EVIDENCE_PLIES` receipts, never all of them."""
    carol = hybrid_keypair_from_seed(b"\x33" * 32)
    receipts = tuple(
        MoveReceipt(
            match_id=MATCH_ID,
            ply=ply,
            seat=WHITE if ply % 2 == 0 else BLACK,
            san="e4",
            received_at_ms=1_000 + ply,
            white_ms=1_800_000 - ply,
            black_ms=1_800_000,
        )
        for ply in range(CLOCK_EVIDENCE_PLIES + 5)
    )
    flag = FlagReceipt(
        match_id=MATCH_ID,
        ply=len(receipts),
        seat=BLACK,
        received_at_ms=9_000,
        white_ms=1_700_000,
        black_ms=0,
    )
    evidence = build_clock_evidence(
        network=NETWORK,
        time_control=FIXED_TIME_CONTROL,
        receipts=receipts,
        final=flag,
        signers=(("sccpt1witness", carol),),
    )
    assert len(evidence.receipts) == CLOCK_EVIDENCE_PLIES
    assert evidence.receipts[0].ply == len(receipts) - CLOCK_EVIDENCE_PLIES
    assert evidence.checkpoint.root == clock_root(NETWORK, evidence.receipts, flag)
    assert evidence.checkpoint.kind == "clock"


def test_a_certificate_move_root_skips_the_opening() -> None:
    certificate = MatchCertificate(
        network=NETWORK,
        match_id=MATCH_ID,
        epoch=3,
        round=0,
        white=WHITE,
        black=BLACK,
        opening=OpeningRef(book_version="v1", line_id=0, hash="11" * 32, moves=("e4", "e5")),
        engine_white="mock-v1",
        engine_black="mock-v1",
        time_control=TimeControl(initial_ms=1_800_000, increment_ms=0),
    )
    san_moves = ["e4", "e5", "Nf3", "Nc6"]
    root = certificate_move_root(certificate, san_moves)
    assert root == move_root(
        NETWORK,
        MATCH_ID,
        {"white": WHITE, "black": BLACK},
        san_moves,
        start_ply=2,
    )


def test_the_checkpoint_payload_is_what_witnesses_sign() -> None:
    checkpoint = StreamCheckpoint(
        network=NETWORK, kind="moves", match_id=MATCH_ID, ply=4, root="22" * 32
    )
    assert checkpoint_payload(checkpoint) == {
        "kind": "moves",
        "match_id": MATCH_ID,
        "network": NETWORK,
        "ply": 4,
        "root": "22" * 32,
    }
