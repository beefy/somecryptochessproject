"""Certificates, result attestations, PGN assembly and transcript storage."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sccp.errors import TranscriptError
from sccp.protocol.models import Transcript
from sccp.protocol.transcript import (
    build_win_proof,
    certificate_payload,
    create_certificate,
    expected_from_certificate,
    load_transcript,
    opening_hash,
    opening_ref,
    pgn_headers,
    proof_from_transcript,
    render_transcript_pgn,
    result_payload,
    save_transcript,
    sign_certificate,
    validate_opening_ref,
    verify_certificate_signatures,
    verify_result_signatures,
)


def test_opening_ref_matches_the_loaded_line(protocol_book) -> None:
    line = protocol_book.line(0)
    ref = opening_ref(line)
    assert ref.book_version == line.book_version
    assert ref.line_id == 0
    assert ref.moves == line.moves
    assert ref.hash == opening_hash(line.book_version, line.line_id, line.moves)
    validate_opening_ref(ref)


def test_validate_opening_ref_rejects_tampering(protocol_book) -> None:
    ref = opening_ref(protocol_book.line(0))
    with pytest.raises(TranscriptError, match="hash"):
        validate_opening_ref(ref.model_copy(update={"hash": "00" * 32}))
    with pytest.raises(TranscriptError):
        validate_opening_ref(ref.model_copy(update={"moves": ("e4", "e5", "Qh9")}))
    with pytest.raises(TranscriptError):
        validate_opening_ref(ref.model_copy(update={"moves": ("e2e4",)}))


def test_create_certificate_carries_the_signed_fields(protocol_matchup, protocol_book) -> None:
    line = protocol_book.line(0)
    certificate = create_certificate(
        protocol_matchup,
        line,
        engine_white="mock-v1:0000000000000001",
        engine_black="mock-v1:0000000000000002",
    )
    assert certificate.network == protocol_matchup.network
    assert certificate.match_id == protocol_matchup.match_id
    assert certificate.white == protocol_matchup.white
    assert certificate.black == protocol_matchup.black
    assert certificate.opening.line_id == 0
    assert certificate.start_signatures == {}


def test_certificate_payload_is_colour_and_network_bound(signed_match) -> None:
    payload = certificate_payload(signed_match.certificate)
    assert payload["network"] == signed_match.params.network_id
    assert payload["match_id"] == signed_match.certificate.match_id
    assert payload["seats"] == sorted(
        (signed_match.certificate.white, signed_match.certificate.black)
    )
    assert payload["colors"] == {
        "white": signed_match.certificate.white,
        "black": signed_match.certificate.black,
    }
    assert payload["engine_fingerprints"] == {
        "white": signed_match.certificate.engine_white,
        "black": signed_match.certificate.engine_black,
    }
    assert "start_signatures" not in payload


def test_certificate_signatures_verify_and_reject_tampering(signed_match) -> None:
    verify_certificate_signatures(signed_match.certificate, signed_match.public_keys)

    missing = signed_match.certificate.model_copy(
        update={"start_signatures": {"white": signed_match.certificate.start_signatures["white"]}}
    )
    with pytest.raises(TranscriptError, match="black"):
        verify_certificate_signatures(missing, signed_match.public_keys)

    tampered = signed_match.certificate.model_copy(update={"engine_white": "mock-v1:dead"})
    with pytest.raises(TranscriptError, match="invalid"):
        verify_certificate_signatures(tampered, signed_match.public_keys)

    with pytest.raises(TranscriptError, match="registered public key"):
        verify_certificate_signatures(signed_match.certificate, {})


def test_signing_requires_the_seat_key(signed_match) -> None:
    carol = signed_match.accounts["carol"]
    with pytest.raises(TranscriptError, match="key belongs to"):
        sign_certificate(signed_match.certificate, "white", carol.keypair)


def test_result_signatures_verify_and_reject_tampering(signed_match) -> None:
    verify_result_signatures(
        signed_match.result,
        signed_match.certificate,
        signed_match.public_keys,
    )
    tampered = signed_match.result.model_copy(update={"plies": signed_match.result.plies + 1})
    with pytest.raises(TranscriptError, match="invalid"):
        verify_result_signatures(
            tampered,
            signed_match.certificate,
            signed_match.public_keys,
        )
    missing = signed_match.result.model_copy(
        update={"result_signatures": {"white": signed_match.result.result_signatures["white"]}}
    )
    with pytest.raises(TranscriptError, match="black"):
        verify_result_signatures(missing, signed_match.certificate, signed_match.public_keys)


def test_result_payload_covers_every_attested_field(signed_match) -> None:
    payload = result_payload(signed_match.result)
    assert payload == {
        "network": signed_match.result.network,
        "match_id": signed_match.result.match_id,
        "game_hash": signed_match.result.game_hash,
        "result": signed_match.result.result,
        "termination": signed_match.result.termination,
        "plies": signed_match.result.plies,
    }


def test_pgn_headers_and_expectations_agree(signed_match) -> None:
    headers = pgn_headers(signed_match.certificate)
    for tag in (
        "Event",
        "Site",
        "Date",
        "Round",
        "White",
        "Black",
        "MatchID",
        "Epoch",
        "RoundNumber",
        "SeatWhite",
        "SeatBlack",
        "EngineWhite",
        "EngineBlack",
        "OpeningID",
        "OpeningHash",
        "Rules",
    ):
        assert headers[tag]
    expected = expected_from_certificate(signed_match.certificate)
    assert expected.match_id == signed_match.certificate.match_id
    assert expected.opening_moves == signed_match.certificate.opening.moves
    assert expected.seat_white == signed_match.certificate.white
    assert expected.seat_black == signed_match.certificate.black
    assert expected.engine_white == signed_match.certificate.engine_white


def test_render_transcript_pgn_round_trips(signed_match) -> None:
    rendered = render_transcript_pgn(
        signed_match.certificate,
        signed_match.parsed.final_board,
        result=signed_match.parsed.result,
        termination=signed_match.parsed.termination,
    )
    assert rendered == signed_match.pgn


def test_build_win_proof_fills_every_field(signed_match) -> None:
    proof = signed_match.proof
    assert proof.network == signed_match.params.network_id
    assert proof.match_id == signed_match.certificate.match_id
    assert proof.game_hash == signed_match.parsed.game_hash
    assert proof.winner == signed_match.certificate.white
    assert proof.loser == signed_match.certificate.black
    assert proof.winner_seat == "white"
    assert proof.opening == signed_match.certificate.opening
    assert proof.pgn == signed_match.pgn
    assert proof.certificate == signed_match.certificate
    assert proof.result_signatures == signed_match.result.result_signatures
    assert proof.quality_summary is None


def test_build_win_proof_requires_both_signatures(signed_match) -> None:
    missing_result = {"white": signed_match.result.result_signatures["white"]}
    with pytest.raises(TranscriptError, match="black result signature"):
        build_win_proof(
            signed_match.certificate,
            signed_match.pgn,
            missing_result,
            params=signed_match.params,
        )
    missing_start = signed_match.certificate.model_copy(
        update={"start_signatures": {"white": signed_match.certificate.start_signatures["white"]}}
    )
    with pytest.raises(TranscriptError, match="black start signature"):
        build_win_proof(
            missing_start,
            signed_match.pgn,
            signed_match.result.result_signatures,
            params=signed_match.params,
        )


def test_build_win_proof_rejects_a_tampered_pgn(signed_match) -> None:
    tampered = signed_match.pgn.replace("Nf6", "Qh9")
    with pytest.raises(ValueError):
        build_win_proof(
            signed_match.certificate,
            tampered,
            signed_match.result.result_signatures,
            params=signed_match.params,
        )


def test_build_win_proof_rejects_the_wrong_network(signed_match) -> None:
    from sccp.config import TESTNET

    with pytest.raises(TranscriptError, match="certificate is for"):
        build_win_proof(
            signed_match.certificate,
            signed_match.pgn,
            signed_match.result.result_signatures,
            params=TESTNET,
        )


def test_transcript_storage_round_trips(signed_match, tmp_path: Path) -> None:
    transcript = Transcript(
        certificate=signed_match.certificate,
        result=signed_match.result,
        pgn=signed_match.pgn,
    )
    path = tmp_path / "nested" / "match.json"
    save_transcript(path, transcript)
    loaded = load_transcript(path)
    assert loaded == transcript
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["certificate"]["match_id"] == signed_match.certificate.match_id


def test_transcript_loading_rejects_bad_files(tmp_path: Path) -> None:
    bad_json = tmp_path / "bad.json"
    bad_json.write_text("{not json", encoding="utf-8")
    with pytest.raises(TranscriptError, match="valid JSON"):
        load_transcript(bad_json)

    bad_shape = tmp_path / "bad-shape.json"
    bad_shape.write_text("{}", encoding="utf-8")
    with pytest.raises(TranscriptError, match="invalid"):
        load_transcript(bad_shape)

    with pytest.raises(TranscriptError, match="cannot read"):
        load_transcript(tmp_path / "missing.json")


def test_proof_from_transcript_matches_the_original(signed_match) -> None:
    transcript = Transcript(
        certificate=signed_match.certificate,
        result=signed_match.result,
        pgn=signed_match.pgn,
    )
    proof = proof_from_transcript(transcript, params=signed_match.params)
    assert proof == signed_match.proof
