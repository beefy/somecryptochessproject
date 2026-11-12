"""Offline win-proof verification and tamper resistance (PROTOCOL.md section 9)."""

from __future__ import annotations

from dataclasses import replace

import pytest

from sccp.chess.openings import books_from_document
from sccp.config import TESTNET
from sccp.errors import ProofVerificationError
from sccp.protocol.models import GameResult, TimeControl
from sccp.protocol.pairing import RosterEntry
from sccp.protocol.transcript import sign_result
from sccp.protocol.verification import (
    ProofFailure,
    context_from_document,
    verify_win_proof,
)
from sccp.serialization import Domain, domain_hex


def _expect_failure(proof, context, reason: ProofFailure) -> None:
    with pytest.raises(ProofVerificationError) as caught:
        verify_win_proof(proof, context)
    assert caught.value.reason is reason, str(caught.value)


def test_happy_path_returns_the_replayed_game(signed_match) -> None:
    verified = verify_win_proof(signed_match.proof, signed_match.context)
    assert verified.proof == signed_match.proof
    assert verified.parsed.game_hash == signed_match.proof.game_hash
    assert verified.parsed.plies == signed_match.proof.plies


def test_book_is_optional(signed_match) -> None:
    context = replace(signed_match.context, book=None)
    assert verify_win_proof(signed_match.proof, context).proof.proof_id


def test_wrong_network_fails(signed_match) -> None:
    context = replace(signed_match.context, params=TESTNET)
    _expect_failure(signed_match.proof, context, ProofFailure.NETWORK)


def test_tampered_beacon_fails(signed_match) -> None:
    beacon = signed_match.beacon.model_copy(update={"hash": "00" * 32})
    context = replace(signed_match.context, beacon=beacon)
    _expect_failure(signed_match.proof, context, ProofFailure.BEACON)


def test_beacon_epoch_mismatch_fails(signed_match) -> None:
    beacon = signed_match.beacon.model_copy(update={"epoch": 4})
    context = replace(signed_match.context, beacon=beacon)
    _expect_failure(signed_match.proof, context, ProofFailure.BEACON)


def test_swapped_colours_fail(signed_match) -> None:
    certificate = signed_match.proof.certificate
    swapped = certificate.model_copy(
        update={"white": certificate.black, "black": certificate.white}
    )
    proof = signed_match.proof.model_copy(
        update={
            "certificate": swapped,
            "winner": certificate.black,
            "loser": certificate.white,
            "winner_seat": "black",
        }
    )
    _expect_failure(proof, signed_match.context, ProofFailure.CERTIFICATE)


def test_non_fixed_time_control_fails(signed_match) -> None:
    certificate = signed_match.proof.certificate.model_copy(
        update={"time_control": TimeControl(initial_ms=60_000, increment_ms=0)}
    )
    proof = signed_match.proof.model_copy(update={"certificate": certificate})
    _expect_failure(proof, signed_match.context, ProofFailure.CERTIFICATE)


def test_changed_opening_fails(signed_match) -> None:
    opening = signed_match.proof.opening.model_copy(update={"moves": ("e4",)})
    certificate = signed_match.proof.certificate.model_copy(update={"opening": opening})
    proof = signed_match.proof.model_copy(update={"opening": opening, "certificate": certificate})
    _expect_failure(proof, signed_match.context, ProofFailure.OPENING)


def test_wrong_book_line_fails(signed_match) -> None:
    books = books_from_document(
        {
            "format": 1,
            "books": {
                signed_match.params.book_version: {
                    "lines": [{"name": "Different", "moves": ["d4", "d5"]}]
                }
            },
        }
    )
    context = replace(signed_match.context, book=books[signed_match.params.book_version])
    _expect_failure(signed_match.proof, context, ProofFailure.OPENING)


def test_swapped_result_fails(signed_match) -> None:
    certificate = signed_match.proof.certificate
    proof = signed_match.proof.model_copy(
        update={
            "result": "0-1",
            "winner": certificate.black,
            "loser": certificate.white,
            "winner_seat": "black",
        }
    )
    _expect_failure(proof, signed_match.context, ProofFailure.SIGNATURE)


def test_missing_opponent_signature_fails(signed_match) -> None:
    proof = signed_match.proof.model_copy(
        update={"result_signatures": {"white": signed_match.proof.result_signatures["white"]}}
    )
    _expect_failure(proof, signed_match.context, ProofFailure.SIGNATURE)


def test_replayed_match_id_fails(signed_match) -> None:
    proof = signed_match.proof.model_copy(update={"match_id": "11" * 32})
    _expect_failure(proof, signed_match.context, ProofFailure.CERTIFICATE)


def test_round_outside_the_epoch_fails(signed_match) -> None:
    certificate = signed_match.proof.certificate.model_copy(update={"round": 99})
    proof = signed_match.proof.model_copy(update={"round": 99, "certificate": certificate})
    _expect_failure(proof, signed_match.context, ProofFailure.CERTIFICATE)


def test_missing_roster_seat_fails(signed_match) -> None:
    only_white = {
        signed_match.certificate.white: signed_match.roster[signed_match.certificate.white]
    }
    context = replace(signed_match.context, roster=only_white)
    _expect_failure(signed_match.proof, context, ProofFailure.ROSTER)


def test_pairing_not_produced_by_the_beacon_fails(signed_match) -> None:
    context = replace(
        signed_match.context,
        quotas={address: 0 for address in signed_match.roster},
    )
    _expect_failure(signed_match.proof, context, ProofFailure.PAIRING)


def test_tampered_pgn_fails(signed_match) -> None:
    proof = signed_match.proof.model_copy(
        update={"pgn": signed_match.proof.pgn.replace("Nf6", "Qh9")}
    )
    _expect_failure(proof, signed_match.context, ProofFailure.PGN)


def test_recomputed_game_hash_mismatch_fails(signed_match) -> None:
    certificate = signed_match.proof.certificate
    bad_hash = "00" * 32
    forged = GameResult(
        network=signed_match.proof.network,
        match_id=signed_match.proof.match_id,
        game_hash=bad_hash,
        result=signed_match.proof.result,
        termination=signed_match.proof.termination,
        plies=signed_match.proof.plies,
    )
    for seat in ("white", "black"):
        address = certificate.white if seat == "white" else certificate.black
        account = next(item for item in signed_match.accounts.values() if item.address == address)
        forged = sign_result(forged, certificate, seat, account.keypair)
    proof = signed_match.proof.model_copy(
        update={
            "game_hash": bad_hash,
            "result_signatures": forged.result_signatures,
            "proof_id": domain_hex(
                Domain.PROOF,
                {
                    "game_hash": bad_hash,
                    "match_id": signed_match.proof.match_id,
                    "winner": signed_match.proof.winner,
                },
            ),
        }
    )
    _expect_failure(proof, signed_match.context, ProofFailure.GAME_HASH)


def test_timeout_claim_id_is_only_valid_for_a_time_forfeit(signed_match) -> None:
    proof = signed_match.proof.model_copy(update={"timeout_claim_id": "00" * 32})
    _expect_failure(proof, signed_match.context, ProofFailure.RESULT)


def test_tampered_proof_id_fails(signed_match) -> None:
    proof = signed_match.proof.model_copy(update={"proof_id": "00" * 32})
    _expect_failure(proof, signed_match.context, ProofFailure.PROOF_ID)


def test_seen_hashes_are_rejected(signed_match) -> None:
    context = replace(
        signed_match.context,
        seen_game_hashes=frozenset({signed_match.proof.game_hash}),
    )
    _expect_failure(signed_match.proof, context, ProofFailure.DUPLICATE)
    context = replace(
        signed_match.context,
        seen_proof_ids=frozenset({signed_match.proof.proof_id}),
    )
    _expect_failure(signed_match.proof, context, ProofFailure.DUPLICATE)


def _context_document(signed_match) -> dict[str, object]:
    return {
        "network": signed_match.params.network_id,
        "beacon": signed_match.beacon.model_dump(mode="json"),
        "roster": {
            address: {"public_key": signed_match.public_keys[address], "max_games": 3}
            for address in signed_match.roster
        },
        "availability": {
            availability.miner: availability.commitment
            for availability in signed_match.availability.values()
        },
        "quotas": {address: 3 for address in signed_match.roster},
        "seen_game_hashes": [],
        "seen_proof_ids": [],
    }


def test_context_document_round_trips(signed_match) -> None:
    context = context_from_document(
        _context_document(signed_match),
        params=signed_match.params,
        book=signed_match.book,
    )
    assert verify_win_proof(signed_match.proof, context).proof.proof_id
    assert context.availability is not None
    assert context.quotas is not None


def test_context_document_defaults_to_the_network_book(signed_match) -> None:
    document = _context_document(signed_match)
    document.pop("quotas")
    context = context_from_document(document, params=signed_match.params)
    assert context.book is not None
    assert context.quotas is None


@pytest.mark.parametrize(
    "mutate",
    [
        lambda document: document.clear(),
        lambda document: document.pop("network"),
        lambda document: document.update(network="nope"),
        lambda document: document.update(beacon={}),
        lambda document: document.update(roster={}),
        lambda document: document["roster"].update({"sccpt1bad": {"public_key": "00"}}),
        lambda document: document.update(quotas={"sccpt1bad": True}),
        lambda document: document.update(seen_game_hashes=["00"]),
        lambda document: document.update(seen_proof_ids=["zz" * 32]),
        lambda document: document.update(availability={"sccpt1bad": "00"}),
    ],
)
def test_context_document_rejects_malformed_inputs(signed_match, mutate) -> None:
    document = _context_document(signed_match)
    mutate(document)
    with pytest.raises(ProofVerificationError) as caught:
        context_from_document(document, params=signed_match.params, book=signed_match.book)
    assert caught.value.reason is ProofFailure.CONTEXT


def test_context_document_rejects_a_network_mismatch(signed_match) -> None:
    with pytest.raises(ProofVerificationError) as caught:
        context_from_document(
            _context_document(signed_match),
            params=TESTNET,
            book=signed_match.book,
        )
    assert caught.value.reason is ProofFailure.CONTEXT


def test_context_roster_members_become_pairing_entries(signed_match) -> None:
    context = context_from_document(
        _context_document(signed_match),
        params=signed_match.params,
        book=signed_match.book,
    )
    for address, entry in context.roster.items():
        assert isinstance(entry, RosterEntry)
        assert entry.address == address
        assert context.public_keys[address] == signed_match.public_keys[address]
