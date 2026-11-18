"""Tampered-record attacks that verification must refuse.

This is the "cheat" half of the adversarial suite: a proof whose bytes do not
match the chain's record. They run with the ``adversarial`` marker, which the
default suite deselects:

    make adversarial

The quality-panel cases that used to live beside them were removed with the
quality layer itself; `docs/design/PROTOCOL.md` section 9.2 records why.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from sccp.errors import ProofVerificationError
from sccp.protocol.verification import ProofFailure, verify_win_proof

pytestmark = pytest.mark.adversarial


def _expect_failure(proof, context, reason: ProofFailure) -> None:
    with pytest.raises(ProofVerificationError) as caught:
        verify_win_proof(proof, context)
    assert caught.value.reason is reason, str(caught.value)


def test_a_tampered_pgn_is_rejected(signed_match) -> None:
    proof = signed_match.proof.model_copy(
        update={"pgn": signed_match.proof.pgn.replace("Nf6", "Qh9")}
    )
    _expect_failure(proof, signed_match.context, ProofFailure.PGN)


def test_a_missing_opponent_signature_is_rejected(signed_match) -> None:
    proof = signed_match.proof.model_copy(
        update={"result_signatures": {"white": signed_match.proof.result_signatures["white"]}}
    )
    _expect_failure(proof, signed_match.context, ProofFailure.SIGNATURE)


def test_a_replayed_match_id_is_rejected(signed_match) -> None:
    proof = signed_match.proof.model_copy(update={"match_id": "11" * 32})
    _expect_failure(proof, signed_match.context, ProofFailure.CERTIFICATE)


def test_a_pairing_the_beacon_did_not_produce_is_rejected(signed_match) -> None:
    context = replace(
        signed_match.context,
        quotas={address: 0 for address in signed_match.roster},
    )
    _expect_failure(signed_match.proof, context, ProofFailure.PAIRING)


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


def test_a_tampered_proof_id_is_rejected(signed_match) -> None:
    proof = signed_match.proof.model_copy(update={"proof_id": "00" * 32})
    _expect_failure(proof, signed_match.context, ProofFailure.PROOF_ID)
