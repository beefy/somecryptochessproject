"""The vectors ``src/web`` is pinned to must still describe this library.

``src/web/src/lib`` re-implements canonical JSON, bech32, Ed25519 signing, match
ids, the colour draw and the game hash in TypeScript. Its vitest suite proves the
ports agree with ``scripts/make_web_vectors.py``; this module proves the checked-in
document still agrees with ``sccp`` itself, so a consensus change cannot leave the
web UI silently pinned to yesterday's rules.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from types import ModuleType

import pytest

from sccp.chain.tx import sign_transaction, tx_body, txid
from sccp.chess.pgn import GameResult, canonical_game_hash
from sccp.crypto.address import encode_address
from sccp.crypto.keys import verify_domain
from sccp.crypto.pq import hybrid_keypair_from_seed, hybrid_verify_domain
from sccp.protocol.beacon import commitment
from sccp.protocol.models import GameResult as ProtocolGameResult
from sccp.protocol.models import MatchCertificate, Transaction, WinProof
from sccp.protocol.pairing import match_id, white_seat
from sccp.protocol.transcript import certificate_payload, result_payload
from sccp.serialization import Domain, canonical_json, domain_hex

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "make_web_vectors.py"
"""The generator whose output is checked in."""


@pytest.fixture(scope="module")
def generator() -> ModuleType:
    """Import ``scripts/make_web_vectors.py`` as a module."""
    spec = importlib.util.spec_from_file_location("sccp_make_web_vectors", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def vectors(generator: ModuleType) -> dict[str, object]:
    """The checked-in vector document, as the web app imports it."""
    return json.loads(generator.target_path().read_text(encoding="utf-8"))


HYBRID_SIGNATURE = re.compile(r"^01[0-9a-f]{6746}$")
"""A well-formed hybrid ledger signature: randomized, so never part of a diff."""


def _stable(value: object) -> object:
    """The document with hybrid signature bytes replaced by a placeholder.

    Ed25519 is deterministic and stays pinned byte for byte, but ML-DSA-65 is
    hedged: signing the same payload twice yields two valid signatures. Pinning
    those bytes would make every regeneration a spurious diff, so the check is
    "everything else is identical, and the signatures verify".
    """
    if isinstance(value, dict):
        return {key: _stable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_stable(item) for item in value]
    if isinstance(value, str) and HYBRID_SIGNATURE.match(value):
        return "<hybrid-signature>"
    return value


def test_checked_in_vectors_match_the_generator(generator: ModuleType) -> None:
    """Regenerating must be a no-op apart from randomized hybrid signatures."""
    expected = _stable(generator.build_vectors())
    actual = _stable(json.loads(generator.target_path().read_text(encoding="utf-8")))
    assert actual == expected, "run python scripts/make_web_vectors.py"


def test_canonical_vectors_describe_this_implementation(vectors: dict[str, object]) -> None:
    """The canonical JSON and its digest are what ``sccp`` produces today."""
    canonical = vectors["canonical"]
    assert isinstance(canonical, dict)
    assert canonical_json(canonical["payload"]).decode("utf-8") == canonical["json"]
    assert domain_hex(Domain.GAME, canonical["payload"]) == canonical["game_digest"]

    bigint = vectors["bigint"]
    assert isinstance(bigint, dict)
    payload = {"big": 2**64 - 1, "neg": -(2**63)}
    assert canonical_json(payload).decode("utf-8") == bigint["json"]
    assert domain_hex(Domain.GAME, payload) == bigint["game_digest"]


def test_key_vectors_describe_this_implementation(vectors: dict[str, object]) -> None:
    """The seed, public key and both addresses still round-trip."""
    keys = vectors["keys"]
    assert isinstance(keys, dict)
    keypair = hybrid_keypair_from_seed(bytes.fromhex(keys["seed_hex"]))
    assert keypair.public_key_hex == keys["hybrid_public_key_hex"]
    assert keypair.ed25519.public_key_hex == keys["public_key_hex"]
    assert encode_address(keypair.public_key_bytes, "testnet") == keys["address_testnet"]
    assert encode_address(keypair.public_key_bytes, "mainnet") == keys["address_mainnet"]


def test_pairing_vectors_describe_this_implementation(vectors: dict[str, object]) -> None:
    """The match id and colour draw are recomputed, not trusted."""
    pairing = vectors["pairing"]
    assert isinstance(pairing, dict)
    seats = pairing["seats"]
    assert pairing["match_id"] == match_id("testnet", pairing["epoch"], pairing["round"], seats)
    beacon = bytes.fromhex(pairing["beacon_hex"])
    assert white_seat(beacon, pairing["match_id"], seats) == pairing["white_seat"]
    # The stored seats are stored unsorted; the match id sorts them.
    assert pairing["match_id"] == match_id(
        "testnet", pairing["epoch"], pairing["round"], list(reversed(seats))
    )


def test_game_vector_describes_this_implementation(vectors: dict[str, object]) -> None:
    """The fool's mate hash still matches the PGN module."""
    game = vectors["game"]
    assert isinstance(game, dict)
    assert game["game_hash"] == canonical_game_hash("testnet", game["san_moves"], GameResult.BLACK)


def test_signature_vectors_verify(vectors: dict[str, object]) -> None:
    """Both signatures in the document verify against it."""
    keys = vectors["keys"]
    move = vectors["move"]
    result = vectors["result"]
    assert isinstance(keys, dict) and isinstance(move, dict) and isinstance(result, dict)
    assert verify_domain(
        bytes.fromhex(keys["public_key_hex"]),
        Domain.MOVE,
        move["payload"],
        bytes.fromhex(move["signature_hex"]),
    )
    # The result vector carries the Ed25519 half (which a browser can check)
    # and the ledger blob (which the chain requires).
    assert verify_domain(
        bytes.fromhex(keys["peer_public_key_hex"]),
        Domain.RESULT,
        result["payload"],
        bytes.fromhex(result["signature_hex"]),
    )
    assert hybrid_verify_domain(
        bytes.fromhex(keys["peer_hybrid_public_key_hex"]),
        Domain.RESULT,
        result["payload"],
        bytes.fromhex(result["ledger_signature_hex"]),
    )


def test_ledger_vectors_describe_this_implementation(vectors: dict[str, object]) -> None:
    """The register transaction and the availability commitment still round-trip."""
    ledger = vectors["ledger"]
    assert isinstance(ledger, dict)
    keypair = hybrid_keypair_from_seed(bytes.fromhex(ledger["seed_hex"]))
    wire = ledger["register"]["transaction"]
    unsigned = Transaction.model_validate({**wire, "signature": None})
    assert txid(unsigned) == ledger["register"]["txid"]
    signed = sign_transaction(keypair, unsigned)
    # ML-DSA-65 is hedged, so re-signing produces a different (valid) blob; the
    # stored one is verified against the tx body below.
    assert signed.signature is not None
    assert hybrid_verify_domain(
        bytes.fromhex(keypair.public_key_hex),
        Domain.TX,
        tx_body(unsigned),
        bytes.fromhex(signed.signature),
    )
    body = tx_body(unsigned)
    # `sign_transaction` signs the txid bytes, which *are* `H("sccp/v1/tx", body)`,
    # so the domain verifier accepts it without a second hashing step.
    assert hybrid_verify_domain(
        bytes.fromhex(keypair.public_key_hex),
        Domain.TX,
        body,
        bytes.fromhex(wire["signature"]),
    )
    assert (
        commitment(
            ledger["availability_commitment"]["address"],
            ledger["availability_commitment"]["epoch"],
            ledger["availability_commitment"]["nonce"],
        )
        == ledger["availability_commitment"]["commitment"]
    )


def test_match_vectors_describe_this_implementation(vectors: dict[str, object]) -> None:
    """The certificate, result and proof in the document are recomputable today."""
    match = vectors["match"]
    assert isinstance(match, dict)
    certificate = MatchCertificate.model_validate(match["certificate"])
    assert certificate_payload(certificate) == match["certificate_payload"]

    for seat, entry in match["seats"].items():
        keypair = hybrid_keypair_from_seed(bytes.fromhex(entry["seed_hex"]))
        assert encode_address(keypair.public_key_bytes, certificate.network) == entry["address"]
        assert entry["public_key_hex"] == keypair.public_key_hex
        assert hybrid_verify_domain(
            bytes.fromhex(keypair.public_key_hex),
            Domain.CERTIFICATE,
            certificate_payload(certificate),
            bytes.fromhex(certificate.start_signatures[seat]),
        )

    result = ProtocolGameResult.model_validate(match["result"])
    assert result_payload(result) == match["result_payload"]
    assert result.game_hash == canonical_game_hash(
        certificate.network, match["san_moves"], GameResult(result.result)
    )

    proof = WinProof.model_validate(match["proof"])
    assert proof.proof_id == domain_hex(
        Domain.PROOF,
        {
            "game_hash": proof.game_hash,
            "match_id": proof.match_id,
            "winner": proof.winner,
        },
    )
    assert proof.match_id == match_id(
        certificate.network,
        certificate.epoch,
        certificate.round,
        [certificate.white, certificate.black],
    )
    assert (
        white_seat(
            bytes.fromhex(match["beacon"]["hash"]),
            certificate.match_id,
            [certificate.white, certificate.black],
        )
        == certificate.white
    )
