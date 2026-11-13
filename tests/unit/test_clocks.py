"""Match clocks, signed moves and clock evidence (PROTOCOL.md section 8.4)."""

from __future__ import annotations

from dataclasses import replace

import chess
import pytest
from pydantic import ValidationError

from sccp.chess.pgn import validate_pgn
from sccp.errors import ClockError, PgnValidationError, ProofVerificationError, TranscriptError
from sccp.protocol.clocks import (
    FIXED_TIME_CONTROL,
    MatchClock,
    build_clock_evidence,
    build_move_evidence,
    flag_result,
    sign_move,
    sign_receipt,
    time_control_from_tag,
    time_control_tag,
    validate_time_control,
    verify_clock_evidence,
    verify_move_evidence,
    verify_receipt_signatures,
    verify_timeout_claim,
)
from sccp.protocol.models import (
    ClockEvidence,
    FlagReceipt,
    MoveReceipt,
    SignedMove,
    TimeControl,
    TimeoutClaim,
)
from sccp.protocol.transcript import (
    build_win_proof,
    create_result,
    expected_from_certificate,
    render_transcript_pgn,
    sign_result,
)
from sccp.protocol.verification import ProofFailure, verify_win_proof


def _account_for(signed_match, address: str):
    return next(account for account in signed_match.accounts.values() if account.address == address)


def _witness(signed_match):
    """The test witness (Carol) as ``(address, keypair)``."""
    carol = signed_match.accounts["carol"]
    return carol.address, carol.keypair


def _evidence(signed_match, receipts, final) -> ClockEvidence:
    """A clock-evidence bundle with Carol's hybrid checkpoint signature."""
    return build_clock_evidence(
        network=signed_match.params.network_id,
        time_control=FIXED_TIME_CONTROL,
        receipts=receipts,
        final=final,
        signers=(_witness(signed_match),),
    )


def _check(signed_match, evidence, **kwargs) -> None:
    """Verify a *mutated* bundle, re-anchoring the checkpoint first.

    Tamper tests change a receipt or the flag and expect the arithmetic
    rule to fire, not the commitment. Re-signing the checkpoint with the
    test witness keeps the commitment honest so the rule under test is
    the one that runs; the commitment itself is covered by the
    dedicated checkpoint tests.
    """
    anchored = _evidence(signed_match, evidence.receipts, evidence.final)
    anchored = anchored.model_copy(update={"time_control": evidence.time_control})
    verify_clock_evidence(anchored, **kwargs)


def _move_evidence(signed_match):
    """The refused-checkmate bundle for the fixture's scholar's mate."""
    return build_move_evidence(
        signed_match.certificate,
        signed_match.parsed.san_moves,
        (_witness(signed_match),),
    )


def _flag_game(signed_match) -> tuple[str, ClockEvidence, object]:
    """A legal one-move game above the opening, with matching clock evidence."""
    certificate = signed_match.certificate
    board = chess.Board()
    for san in certificate.opening.moves:
        board.push(board.parse_san(san))
    board.push(board.parse_san("Qh5"))
    pgn = render_transcript_pgn(
        certificate,
        board,
        result="1-0",
        termination="time_forfeit",
    )

    witness = signed_match.accounts["carol"].keypair
    network = signed_match.params.network_id
    move_ply = len(certificate.opening.moves)
    receipt = sign_receipt(
        MoveReceipt(
            match_id=certificate.match_id,
            ply=move_ply,
            seat=certificate.white,
            san="Qh5",
            received_at_ms=1_000_000,
            white_ms=1_790_000,
            black_ms=FIXED_TIME_CONTROL.initial_ms,
        ),
        witness,
        network=network,
    )
    final = sign_receipt(
        FlagReceipt(
            match_id=certificate.match_id,
            ply=move_ply + 1,
            seat=certificate.black,
            received_at_ms=1_000_000 + FIXED_TIME_CONTROL.initial_ms + 250,
            white_ms=1_790_000,
            black_ms=0,
        ),
        witness,
        network=network,
    )
    return pgn, _evidence(signed_match, (receipt,), final)


def _long_flag_game(signed_match) -> tuple[str, ClockEvidence]:
    """A seven-ply game whose receipts exercise the running-clock arithmetic."""
    certificate = signed_match.certificate
    board = chess.Board()
    for san in certificate.opening.moves:
        board.push(board.parse_san(san))
    for san in ("Qh5", "Nf6", "Qh3"):
        board.push(board.parse_san(san))
    pgn = render_transcript_pgn(
        certificate,
        board,
        result="1-0",
        termination="time_forfeit",
    )

    witness = signed_match.accounts["carol"].keypair
    network = signed_match.params.network_id
    opening_plies = len(certificate.opening.moves)
    clocks = (
        (1_790_000, 1_800_000),
        (1_790_000, 1_799_250),
        (1_789_250, 1_799_250),
    )
    receipts = tuple(
        sign_receipt(
            MoveReceipt(
                match_id=certificate.match_id,
                ply=opening_plies + offset,
                seat=(
                    certificate.white if (opening_plies + offset) % 2 == 0 else certificate.black
                ),
                san=("Qh5", "Nf6", "Qh3")[offset],
                received_at_ms=1_000_000 + offset * 1_000,
                white_ms=white_ms,
                black_ms=black_ms,
            ),
            witness,
            network=network,
        )
        for offset, (white_ms, black_ms) in enumerate(clocks)
    )
    final = sign_receipt(
        FlagReceipt(
            match_id=certificate.match_id,
            ply=opening_plies + 3,
            seat=certificate.black,
            received_at_ms=1_000_000 + 2_000 + 1_799_250 + 250,
            white_ms=1_789_250,
            black_ms=0,
        ),
        witness,
        network=network,
    )
    return pgn, _evidence(signed_match, receipts, final)


def _signed_result(signed_match, pgn: str):
    parsed = validate_pgn(
        pgn,
        params=signed_match.params,
        expected=expected_from_certificate(signed_match.certificate),
        require_win=True,
        clock_evidence=object(),
    )
    result = create_result(signed_match.certificate, parsed)
    for seat in ("white", "black"):
        address = (
            signed_match.certificate.white if seat == "white" else signed_match.certificate.black
        )
        result = sign_result(
            result,
            signed_match.certificate,
            seat,
            _account_for(signed_match, address).keypair,
        )
    return parsed, result


def test_time_control_tag_is_lossless() -> None:
    assert time_control_tag(FIXED_TIME_CONTROL) == "1800+0"
    assert time_control_from_tag("1800+0") == FIXED_TIME_CONTROL
    assert time_control_from_tag("1800+5") == TimeControl(initial_ms=1_800_000, increment_ms=5_000)
    for bad in (
        "1800",
        "1800+",
        "+0",
        "1800.5+0",
        "1800-5",
        "-1+0",
        "0+0",
        "1_800+0",
        " 1800+0",
        "86401+0",
        "1800+1000",
    ):
        with pytest.raises(ClockError, match="TimeControl"):
            time_control_from_tag(bad)


def test_validate_time_control_enforces_bounds_and_the_fixed_control() -> None:
    validate_time_control(FIXED_TIME_CONTROL, max_plies=300, require_fixed=True)
    with pytest.raises(ClockError, match="protocol-fixed"):
        validate_time_control(
            TimeControl(initial_ms=60_000, increment_ms=0),
            max_plies=300,
            require_fixed=True,
        )
    with pytest.raises(ClockError, match="exceeds"):
        validate_time_control(
            TimeControl(initial_ms=85_000_000, increment_ms=1_000),
            max_plies=2_000,
        )

    # model_validate must run the same cross-field bound as the constructor.
    with pytest.raises(ValidationError, match="MAX_CLOCK_MS"):
        TimeControl.model_validate({"initial_ms": 86_000_000, "increment_ms": 1_000})


def test_match_clock_spends_mover_time_and_applies_increment() -> None:
    control = TimeControl(initial_ms=10_000, increment_ms=3_000)
    clock = MatchClock.start(control, now_ms=0, lag_grace_ms=250)
    assert clock.snapshot(0) == {"white": 10_000, "black": 10_000}

    remaining = clock.apply_move(1_000)
    assert remaining == 10_000 - 750 + 3_000
    assert clock.turn == "black"
    assert clock.snapshot(1_000) == {"white": remaining, "black": 10_000}

    # A clock source that steps backwards never creates time.
    assert clock.current_remaining_ms(0) == 10_000

    # Nor may a backwards move timestamp move the next turn's start backwards.
    assert clock.apply_move(0) == 13_000
    assert clock.turn == "white"
    assert clock.current_remaining_ms(3_000) == 10_500


def test_match_clock_flags_at_zero_and_refuses_a_late_move() -> None:
    clock = MatchClock.start(now_ms=0)
    assert clock.is_flagged(FIXED_TIME_CONTROL.initial_ms + 249) is False
    assert clock.flag(FIXED_TIME_CONTROL.initial_ms + 250) == "white"
    assert clock.snapshot(FIXED_TIME_CONTROL.initial_ms + 250) == {
        "white": 0,
        "black": FIXED_TIME_CONTROL.initial_ms,
    }
    with pytest.raises(ClockError, match="no time left"):
        clock.apply_move(FIXED_TIME_CONTROL.initial_ms + 250)


def test_match_clock_rejects_a_clock_that_was_never_started() -> None:
    clock = MatchClock.start(now_ms=0)
    clock.started = False
    with pytest.raises(ClockError, match="not started"):
        clock.apply_move(1)


def test_match_clock_validates_its_time_source_and_both_turns() -> None:
    with pytest.raises(ClockError, match="lag_grace_ms"):
        MatchClock.start(now_ms=0, lag_grace_ms=1_001)
    with pytest.raises(ClockError, match="now_ms"):
        MatchClock.start(now_ms=-1)

    clock = MatchClock.start(TimeControl(initial_ms=10_000, increment_ms=3_000), now_ms=0)
    with pytest.raises(ClockError, match="now_ms"):
        clock.elapsed_ms(-1)
    assert clock.flag(0) is None

    assert clock.apply_move(1_000) == 12_250
    assert clock.turn == "black"
    assert clock.apply_move(2_000) == 12_250
    assert clock.snapshot(2_000) == {"white": 12_250, "black": 12_250}
    assert clock.flag(100_000) == "white"


def test_flag_result_follows_fide_insufficient_material() -> None:
    assert flag_result(chess.Board(), flagged="white") == "0-1"
    assert flag_result(chess.Board(), flagged="black") == "1-0"
    kings_only = chess.Board("8/8/8/8/8/8/8/k6K w - - 0 1")
    assert flag_result(kings_only, flagged="white") == "1/2-1/2"

    # A lone bishop cannot force mate, but the opponent's rook can block an
    # escape, so only a dead position (both sides insufficient) is a draw.
    playable = chess.Board("r7/8/8/8/8/8/8/k1B4K w - - 0 1")
    assert playable.has_insufficient_material(chess.WHITE) is True
    assert playable.is_insufficient_material() is False
    assert flag_result(playable, flagged="black") == "1-0"


def test_clock_evidence_verifies_and_rejects_tampering(signed_match) -> None:
    pgn, evidence = _flag_game(signed_match)
    parsed, _ = _signed_result(signed_match, pgn)
    kwargs = {
        "certificate": signed_match.certificate,
        "san_moves": parsed.san_moves,
        "result": "1-0",
        "winner_seat": "white",
        "public_keys": signed_match.public_keys,
        "quorum": signed_match.params.clock_witness_quorum,
        "max_plies": signed_match.params.max_plies,
    }
    verify_clock_evidence(evidence, **kwargs)

    broken = evidence.model_copy(
        update={"final": evidence.final.model_copy(update={"black_ms": 1})}
    )
    with pytest.raises(ClockError, match="not zero"):
        _check(signed_match, broken, **kwargs)

    retimed = evidence.model_copy(
        update={"time_control": TimeControl(initial_ms=60_000, increment_ms=0)}
    )
    with pytest.raises(ClockError, match="protocol-fixed"):
        _check(signed_match, retimed, **kwargs)

    unsigned = evidence.model_copy(
        update={"checkpoint": evidence.checkpoint.model_copy(update={"witnesses": ()})}
    )
    with pytest.raises(ClockError, match="quorum"):
        verify_clock_evidence(unsigned, **kwargs)

    forged = evidence.model_copy(
        update={
            "checkpoint": evidence.checkpoint.model_copy(
                update={
                    "witnesses": (
                        evidence.checkpoint.witnesses[0].model_copy(
                            update={"signature": "00" * 6748}
                        ),
                    )
                }
            )
        }
    )
    with pytest.raises(ClockError, match="invalid checkpoint signature"):
        verify_clock_evidence(forged, **kwargs)

    other_match = evidence.model_copy(
        update={"final": evidence.final.model_copy(update={"match_id": "00" * 32})}
    )
    with pytest.raises(ClockError, match="does not belong"):
        _check(signed_match, other_match, **kwargs)
    with pytest.raises(ClockError, match="no-show"):
        _check(signed_match, evidence, **{**kwargs, "san_moves": ()})
    wrong_ply = evidence.model_copy(
        update={"final": evidence.final.model_copy(update={"ply": evidence.final.ply + 1})}
    )
    with pytest.raises(ClockError, match="flag receipt is for ply"):
        _check(signed_match, wrong_ply, **kwargs)
    wrong_final_seat = evidence.model_copy(
        update={"final": evidence.final.model_copy(update={"seat": signed_match.certificate.white})}
    )
    with pytest.raises(ClockError, match="side to move"):
        _check(signed_match, wrong_final_seat, **kwargs)
    with pytest.raises(ClockError, match="does not agree"):
        _check(signed_match, evidence, **{**kwargs, "result": "0-1"})
    empty_clocks = evidence.model_copy(
        update={"final": evidence.final.model_copy(update={"white_ms": 0, "black_ms": 0})}
    )
    with pytest.raises(ClockError, match="winner's clock"):
        _check(signed_match, empty_clocks, **kwargs)
    with pytest.raises(ClockError, match="no move receipts"):
        _check(signed_match, evidence.model_copy(update={"receipts": ()}), **kwargs)

    retimed_but_required_off = evidence.model_copy(
        update={"time_control": TimeControl(initial_ms=60_000, increment_ms=0)}
    )
    with pytest.raises(ClockError, match="does not match the certificate"):
        _check(signed_match, retimed_but_required_off, require_fixed=False, **kwargs)


def test_receipt_witnesses_must_be_unique_registered_and_quorate(signed_match) -> None:
    _, evidence = _flag_game(signed_match)
    final = evidence.final
    witness = final.witnesses[0]

    with pytest.raises(ClockError, match="more than once"):
        verify_receipt_signatures(
            final.model_copy(update={"witnesses": (witness, witness)}),
            network=signed_match.params.network_id,
            public_keys=signed_match.public_keys,
            quorum=1,
        )
    with pytest.raises(ClockError, match="at least one"):
        verify_receipt_signatures(
            final,
            network=signed_match.params.network_id,
            public_keys=signed_match.public_keys,
            quorum=0,
        )
    with pytest.raises(ClockError, match="no registered public key"):
        verify_receipt_signatures(
            final,
            network=signed_match.params.network_id,
            public_keys={},
            quorum=1,
        )


def test_clock_receipts_follow_monotonic_running_clocks(signed_match) -> None:
    pgn, evidence = _long_flag_game(signed_match)
    parsed, _ = _signed_result(signed_match, pgn)
    kwargs = {
        "certificate": signed_match.certificate,
        "san_moves": parsed.san_moves,
        "result": "1-0",
        "winner_seat": "white",
        "public_keys": signed_match.public_keys,
        "quorum": signed_match.params.clock_witness_quorum,
        "max_plies": signed_match.params.max_plies,
    }
    verify_clock_evidence(evidence, **kwargs)

    out_of_order = evidence.model_copy(
        update={
            "receipts": (
                evidence.receipts[0],
                evidence.receipts[1].model_copy(
                    update={"received_at_ms": evidence.receipts[0].received_at_ms - 1}
                ),
                evidence.receipts[2],
            )
        }
    )
    with pytest.raises(ClockError, match="monotonic"):
        _check(signed_match, out_of_order, **kwargs)

    witness = signed_match.accounts["carol"].keypair
    bad_clock = sign_receipt(
        evidence.receipts[1].model_copy(
            update={"black_ms": evidence.receipts[1].black_ms + 1, "witnesses": ()}
        ),
        witness,
        network=signed_match.params.network_id,
    )
    wrong_clock = evidence.model_copy(
        update={
            "receipts": (
                evidence.receipts[0],
                bad_clock,
                evidence.receipts[2],
            )
        }
    )
    with pytest.raises(ClockError, match="expected"):
        _check(signed_match, wrong_clock, **kwargs)


def test_clock_evidence_rejects_out_of_range_and_early_flags(signed_match) -> None:
    pgn, evidence = _long_flag_game(signed_match)
    parsed, _ = _signed_result(signed_match, pgn)
    kwargs = {
        "certificate": signed_match.certificate,
        "san_moves": parsed.san_moves,
        "result": "1-0",
        "winner_seat": "white",
        "public_keys": signed_match.public_keys,
        "quorum": signed_match.params.clock_witness_quorum,
        "max_plies": signed_match.params.max_plies,
    }
    witness = signed_match.accounts["carol"].keypair
    network = signed_match.params.network_id

    def resign(receipt):
        blank = receipt.model_copy(update={"witnesses": ()})
        return sign_receipt(blank, witness, network=network)

    wrong_match = evidence.receipts[1].model_copy(update={"match_id": "00" * 32})
    with pytest.raises(ClockError, match="does not belong"):
        _check(
            signed_match,
            evidence.model_copy(
                update={"receipts": (evidence.receipts[0], wrong_match, evidence.receipts[2])}
            ),
            **kwargs,
        )
    wrong_san = evidence.receipts[1].model_copy(update={"san": "Qh5"})
    with pytest.raises(ClockError, match="receipt SAN at ply"):
        _check(
            signed_match,
            evidence.model_copy(
                update={"receipts": (evidence.receipts[0], wrong_san, evidence.receipts[2])}
            ),
            **kwargs,
        )
    wrong_seat = evidence.receipts[1].model_copy(update={"seat": signed_match.certificate.white})
    with pytest.raises(ClockError, match="belongs to black"):
        _check(
            signed_match,
            evidence.model_copy(
                update={"receipts": (evidence.receipts[0], wrong_seat, evidence.receipts[2])}
            ),
            **kwargs,
        )

    zero_black = evidence.receipts[0].model_copy(update={"black_ms": 0})
    with pytest.raises(ClockError, match="black clock is out of range"):
        _check(
            signed_match,
            evidence.model_copy(update={"receipts": (resign(zero_black), *evidence.receipts[1:])}),
            **kwargs,
        )

    zero_first = evidence.receipts[0].model_copy(update={"white_ms": 0})
    with pytest.raises(ClockError, match="out of range"):
        _check(
            signed_match,
            evidence.model_copy(update={"receipts": (resign(zero_first), *evidence.receipts[1:])}),
            **kwargs,
        )

    changed_idle = evidence.receipts[0].model_copy(update={"black_ms": 1_799_999})
    with pytest.raises(ClockError, match="idle side"):
        _check(
            signed_match,
            evidence.model_copy(
                update={"receipts": (resign(changed_idle), *evidence.receipts[1:])}
            ),
            **kwargs,
        )

    late_black = evidence.receipts[1].model_copy(
        update={"received_at_ms": 2_800_250, "black_ms": 0}
    )
    with pytest.raises(ClockError, match="flagged before"):
        _check(
            signed_match,
            evidence.model_copy(
                update={
                    "receipts": (
                        evidence.receipts[0],
                        resign(late_black),
                        evidence.receipts[2],
                    )
                }
            ),
            **kwargs,
        )

    changed_later_idle = evidence.receipts[2].model_copy(update={"black_ms": 1_799_251})
    with pytest.raises(ClockError, match="idle side"):
        _check(
            signed_match,
            evidence.model_copy(
                update={
                    "receipts": (
                        evidence.receipts[0],
                        evidence.receipts[1],
                        resign(changed_later_idle),
                    )
                }
            ),
            **kwargs,
        )

    early_final = evidence.final.model_copy(
        update={"received_at_ms": evidence.receipts[-1].received_at_ms}
    )
    with pytest.raises(ClockError, match="before the flagged clock"):
        _check(signed_match, evidence.model_copy(update={"final": resign(early_final)}), **kwargs)

    changed_winner = evidence.final.model_copy(update={"white_ms": evidence.final.white_ms + 1})
    with pytest.raises(ClockError, match="changed the winner's clock"):
        _check(
            signed_match, evidence.model_copy(update={"final": resign(changed_winner)}), **kwargs
        )

    predated = evidence.final.model_copy(
        update={"received_at_ms": evidence.receipts[-1].received_at_ms - 1}
    )
    with pytest.raises(ClockError, match="predates"):
        _check(signed_match, evidence.model_copy(update={"final": resign(predated)}), **kwargs)


def test_time_forfeit_proof_round_trips(signed_match) -> None:
    pgn, evidence = _flag_game(signed_match)
    _, result = _signed_result(signed_match, pgn)
    proof = build_win_proof(
        signed_match.certificate,
        pgn,
        result.result_signatures,
        params=signed_match.params,
        clock_evidence=evidence,
    )
    assert proof.termination == "time_forfeit"
    assert proof.low_ply is False
    assert proof.clock_evidence == evidence
    verified = verify_win_proof(proof, signed_match.context)
    assert verified.proof == proof
    assert verified.parsed.termination.value == "time_forfeit"


def test_low_ply_forfeits_are_marked_and_verified(signed_match) -> None:
    pgn, evidence = _flag_game(signed_match)
    _, result = _signed_result(signed_match, pgn)
    params = signed_match.params.model_copy(update={"min_plies": 10})
    proof = build_win_proof(
        signed_match.certificate,
        pgn,
        result.result_signatures,
        params=params,
        clock_evidence=evidence,
    )
    assert proof.low_ply is True
    context = replace(signed_match.context, params=params)
    assert verify_win_proof(proof, context).proof.low_ply is True


def test_refused_time_forfeit_needs_the_claim_id(signed_match) -> None:
    pgn, evidence = _flag_game(signed_match)
    _, result = _signed_result(signed_match, pgn)
    white_only = {"white": result.result_signatures["white"]}

    with pytest.raises(TranscriptError, match="black result signature"):
        build_win_proof(
            signed_match.certificate,
            pgn,
            white_only,
            params=signed_match.params,
            clock_evidence=evidence,
        )

    claim_id = "ab" * 32
    proof = build_win_proof(
        signed_match.certificate,
        pgn,
        white_only,
        params=signed_match.params,
        clock_evidence=evidence,
        timeout_claim_id=claim_id,
    )
    assert proof.timeout_claim_id == claim_id
    assert verify_win_proof(proof, signed_match.context).proof == proof

    # Without the committed claim, the missing seat cannot be excused.
    tampered = proof.model_copy(update={"timeout_claim_id": None})
    with pytest.raises(ProofVerificationError) as caught:
        verify_win_proof(tampered, signed_match.context)
    assert caught.value.reason is ProofFailure.SIGNATURE


def test_time_forfeit_requires_evidence_and_rejects_move_evidence(signed_match) -> None:
    pgn, evidence = _flag_game(signed_match)
    _, result = _signed_result(signed_match, pgn)
    with pytest.raises(PgnValidationError, match="time-forfeit"):
        build_win_proof(
            signed_match.certificate,
            pgn,
            result.result_signatures,
            params=signed_match.params,
        )

    proof = build_win_proof(
        signed_match.certificate,
        pgn,
        result.result_signatures,
        params=signed_match.params,
        clock_evidence=evidence,
    )
    move = sign_move(
        SignedMove(
            network=signed_match.certificate.network,
            match_id=signed_match.certificate.match_id,
            seat=signed_match.certificate.white,
            ply=len(signed_match.certificate.opening.moves),
            san="Qh5",
            signature="00" * 64,
        ),
        _account_for(signed_match, signed_match.certificate.white).keypair,
    )
    tampered = proof.model_copy(update={"move_evidence": (move,)})
    with pytest.raises(ProofVerificationError) as caught:
        verify_win_proof(tampered, signed_match.context)
    assert caught.value.reason is ProofFailure.RESULT


def test_timeout_claim_validates_its_fields(signed_match) -> None:
    pgn, evidence = _flag_game(signed_match)
    parsed, _ = _signed_result(signed_match, pgn)
    certificate = signed_match.certificate
    last_ply = len(parsed.san_moves) - 1
    last_move = sign_move(
        SignedMove(
            network=certificate.network,
            match_id=certificate.match_id,
            seat=certificate.white,
            ply=last_ply,
            san=parsed.san_moves[last_ply],
            signature="00" * 64,
        ),
        _account_for(signed_match, certificate.white).keypair,
    )
    claim = TimeoutClaim(
        network=certificate.network,
        match_id=certificate.match_id,
        certificate=certificate,
        last_move=last_move,
        clock_evidence=evidence,
        bond=1,
    )
    kwargs = {
        "certificate": certificate,
        "san_moves": parsed.san_moves,
        "public_keys": signed_match.public_keys,
        "quorum": signed_match.params.clock_witness_quorum,
        "max_plies": signed_match.params.max_plies,
    }
    assert verify_timeout_claim(claim, **kwargs) == "white"

    with pytest.raises(ClockError, match="positive bond"):
        verify_timeout_claim(claim.model_copy(update={"bond": 0}), **kwargs)

    wrong = certificate.model_copy(update={"match_id": "00" * 32})
    with pytest.raises(ClockError, match="does not belong"):
        verify_timeout_claim(claim, **{**kwargs, "certificate": wrong})
    with pytest.raises(ClockError, match="different networks"):
        verify_timeout_claim(claim.model_copy(update={"network": "mainnet"}), **kwargs)
    with pytest.raises(ClockError, match="embeds a different certificate"):
        verify_timeout_claim(claim.model_copy(update={"certificate": wrong}), **kwargs)
    with pytest.raises(ClockError, match="at least one played move"):
        verify_timeout_claim(claim, **{**kwargs, "san_moves": ()})
    with pytest.raises(ClockError, match="last move is ply"):
        bad_ply = claim.model_copy(
            update={"last_move": last_move.model_copy(update={"ply": last_ply - 1})}
        )
        verify_timeout_claim(bad_ply, **kwargs)
    with pytest.raises(ClockError, match="last move SAN"):
        bad_san = claim.model_copy(
            update={"last_move": last_move.model_copy(update={"san": "Nf6"})}
        )
        verify_timeout_claim(bad_san, **kwargs)
    with pytest.raises(ClockError, match="belongs to white"):
        wrong_seat = claim.model_copy(
            update={"last_move": last_move.model_copy(update={"seat": certificate.black})}
        )
        verify_timeout_claim(wrong_seat, **kwargs)
    stranger = evidence.final.model_copy(update={"seat": "sccpt1stranger"})
    bad_flag = claim.model_copy(
        update={"clock_evidence": evidence.model_copy(update={"final": stranger})}
    )
    with pytest.raises(ClockError, match="does not name a seat"):
        verify_timeout_claim(bad_flag, **kwargs)


def test_refused_checkmate_needs_a_hybrid_checkpoint(signed_match) -> None:
    certificate = signed_match.certificate
    evidence = _move_evidence(signed_match)
    white_only = {"white": signed_match.result.result_signatures["white"]}
    proof = build_win_proof(
        certificate,
        signed_match.pgn,
        white_only,
        params=signed_match.params,
        move_evidence=evidence,
    )
    assert proof.move_evidence == evidence
    assert verify_win_proof(proof, signed_match.context).proof == proof

    # Dropping a ply changes the committed root, so the checkpoint no longer
    # covers the evidence.
    tampered = proof.model_copy(
        update={"move_evidence": evidence.model_copy(update={"moves": evidence.moves[:-1]})}
    )
    with pytest.raises(ProofVerificationError) as caught:
        verify_win_proof(tampered, signed_match.context)
    assert caught.value.reason is ProofFailure.RESULT


def test_move_evidence_validates_order_authorship_and_commitment(signed_match) -> None:
    certificate = signed_match.certificate
    evidence = _move_evidence(signed_match)
    kwargs = {
        "certificate": certificate,
        "public_keys": signed_match.public_keys,
        "san_moves": signed_match.parsed.san_moves,
        "quorum": signed_match.params.clock_witness_quorum,
    }
    verify_move_evidence(evidence, **kwargs)

    def with_moves(moves) -> object:
        return evidence.model_copy(update={"moves": tuple(moves)})

    def with_checkpoint(**updates) -> object:
        return evidence.model_copy(
            update={"checkpoint": evidence.checkpoint.model_copy(update=updates)}
        )

    with pytest.raises(ClockError, match="empty"):
        verify_move_evidence(None, **kwargs)
    with pytest.raises(ClockError, match="cover every"):
        verify_move_evidence(with_moves(evidence.moves[1:]), **kwargs)
    with pytest.raises(ClockError, match="SAN at ply"):
        verify_move_evidence(
            with_moves(
                (
                    evidence.moves[0].model_copy(update={"san": "Nf6"}),
                    *evidence.moves[1:],
                )
            ),
            **kwargs,
        )
    other_seat = (
        certificate.black if evidence.moves[0].seat == certificate.white else certificate.white
    )
    with pytest.raises(ClockError, match="was played by"):
        verify_move_evidence(
            with_moves(
                (
                    evidence.moves[0].model_copy(update={"seat": other_seat}),
                    *evidence.moves[1:],
                )
            ),
            **kwargs,
        )
    with pytest.raises(ClockError, match="another network"):
        verify_move_evidence(with_checkpoint(network="mainnet"), **kwargs)
    with pytest.raises(ClockError, match="does not belong"):
        verify_move_evidence(with_checkpoint(match_id="00" * 32), **kwargs)
    with pytest.raises(ClockError, match="expected 'moves'"):
        verify_move_evidence(with_checkpoint(kind="clock"), **kwargs)
    with pytest.raises(ClockError, match="commits to"):
        verify_move_evidence(with_checkpoint(ply=99), **kwargs)
    with pytest.raises(ClockError, match="root does not cover"):
        verify_move_evidence(with_checkpoint(root="00" * 32), **kwargs)
    with pytest.raises(ClockError, match="quorum"):
        verify_move_evidence(with_checkpoint(witnesses=()), **kwargs)
    with pytest.raises(ClockError, match="invalid checkpoint signature"):
        verify_move_evidence(
            with_checkpoint(
                witnesses=(
                    evidence.checkpoint.witnesses[0].model_copy(update={"signature": "00" * 6748}),
                )
            ),
            **kwargs,
        )
