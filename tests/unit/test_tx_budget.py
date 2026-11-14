"""The size budget of PROTOCOL.md section 12.5, measured rather than estimated.

The largest transaction the protocol can produce is a 400-ply `timeout_claim` on
a 400-ply mainnet game: a certificate, the claimant's last move, a full
`CLOCK_EVIDENCE_PLIES` receipt window with a three-witness hybrid checkpoint, and
the claim itself. This test builds exactly that and asserts it fits, so a future
field or signature cannot quietly reintroduce the overflow.
"""

from __future__ import annotations

from sccp.chain.tx import tx_size
from sccp.config import CLOCK_EVIDENCE_PLIES, MAINNET
from sccp.crypto.address import encode_address
from sccp.crypto.pq import hybrid_keypair_from_seed
from sccp.protocol.clocks import (
    FIXED_TIME_CONTROL,
    build_checkpoint,
    build_clock_evidence,
    sign_move,
)
from sccp.protocol.commitments import clock_root
from sccp.protocol.models import (
    ClockEvidence,
    FlagReceipt,
    Matchup,
    MoveReceipt,
    OpeningRef,
    SignedMove,
    TimeoutClaim,
    Transaction,
    TxType,
)
from sccp.protocol.transcript import create_certificate, sign_certificate
from sccp.serialization import Domain, domain_hex

PLIES = MAINNET.max_plies
MATCH_ID = "49e2b53a34307bb42d95c210b5756a71b877243c54f4306e886e3eff0d5d0f6e"
OPENING = ["e4", "e5", "Nf3", "Nc6"]


def _claim() -> tuple[Transaction, ClockEvidence]:
    network = MAINNET.network_id
    white = hybrid_keypair_from_seed(b"\x01" * 32)
    black = hybrid_keypair_from_seed(b"\x02" * 32)
    seats = {
        "white": encode_address(white.public_key_bytes, MAINNET),
        "black": encode_address(black.public_key_bytes, MAINNET),
    }
    witnesses = tuple(
        (
            encode_address(
                hybrid_keypair_from_seed(bytes([10 + index]) * 32).public_key_bytes, MAINNET
            ),
            hybrid_keypair_from_seed(bytes([10 + index]) * 32),
        )
        for index in range(3)
    )

    matchup = Matchup(
        network=network,
        match_id=MATCH_ID,
        epoch=3,
        round=0,
        white=seats["white"],
        black=seats["black"],
    )
    opening = OpeningRef(
        book_version="v1",
        line_id=0,
        hash=domain_hex(Domain.OPENING, {"book_version": "v1", "line_id": 0, "moves": OPENING}),
        moves=tuple(OPENING),
    )
    certificate = create_certificate(
        matchup, opening, engine_white="mock-v1", engine_black="mock-v1"
    )
    certificate = sign_certificate(certificate, "white", white)
    certificate = sign_certificate(certificate, "black", black)

    receipts = tuple(
        MoveReceipt(
            match_id=MATCH_ID,
            ply=ply,
            seat=seats["white"] if ply % 2 == 0 else seats["black"],
            san="Nf3",
            received_at_ms=1_000_000 + ply * 4_000,
            white_ms=1_800_000 - ply * 2_000,
            black_ms=1_800_000 - ply * 2_000,
        )
        for ply in range(len(OPENING), PLIES)
    )
    final = FlagReceipt(
        match_id=MATCH_ID,
        ply=PLIES,
        seat=seats["black"],
        received_at_ms=1_000_000 + PLIES * 4_000,
        white_ms=1_000,
        black_ms=0,
    )
    evidence = build_clock_evidence(
        network=network,
        time_control=FIXED_TIME_CONTROL,
        receipts=receipts,
        final=final,
        signers=witnesses,
    )
    last_move = sign_move(
        SignedMove(
            network=network,
            match_id=MATCH_ID,
            seat=seats["white"],
            ply=PLIES - 1,
            san="Nf3",
            signature="00" * 64,
        ),
        white,
    )
    claim = TimeoutClaim(
        network=network,
        match_id=MATCH_ID,
        certificate=certificate,
        last_move=last_move,
        clock_evidence=evidence,
        bond=MAINNET.min_timeout_bond,
    )
    return (
        Transaction(
            network=network,
            type=TxType.TIMEOUT_CLAIM,
            sender=seats["white"],
            nonce=0,
            fee=MAINNET.min_fee,
            payload=claim.model_dump(mode="json"),
        ),
        evidence,
        receipts,
        final,
        witnesses,
        certificate,
    )


def test_the_largest_transaction_fits_the_cap() -> None:
    tx, evidence, _receipts, _final, _witnesses, _certificate = _claim()
    assert len(evidence.receipts) == CLOCK_EVIDENCE_PLIES
    size = tx_size(tx)
    assert size < MAINNET.max_tx_bytes, (
        f"the largest timeout_claim is {size} bytes, cap is {MAINNET.max_tx_bytes}"
    )
    # Room for a larger quorum or window without another consensus change.
    assert MAINNET.max_tx_bytes - size > 32_768


def test_an_unwindowed_claim_would_overflow() -> None:
    """The window is not cosmetic: the full receipt list does not fit."""
    tx, _evidence, receipts, final, witnesses, certificate = _claim()
    wide = ClockEvidence(
        time_control=FIXED_TIME_CONTROL,
        final=final,
        receipts=receipts,
        checkpoint=build_checkpoint(
            network=MAINNET.network_id,
            kind="clock",
            match_id=MATCH_ID,
            ply=final.ply,
            root=clock_root(MAINNET.network_id, receipts, final),
            signers=witnesses,
        ),
    )
    claim = TimeoutClaim.model_validate(tx.payload)
    wide_tx = tx.model_copy(
        update={
            "payload": claim.model_copy(update={"clock_evidence": wide}).model_dump(mode="json")
        }
    )
    assert len(receipts) > CLOCK_EVIDENCE_PLIES
    assert tx_size(wide_tx) > 65_536
    assert certificate.match_id == MATCH_ID
