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
import sys
from pathlib import Path
from types import ModuleType

import pytest

from sccp.chess.pgn import GameResult, canonical_game_hash
from sccp.crypto.address import encode_address
from sccp.crypto.keys import keypair_from_seed, verify_domain
from sccp.protocol.pairing import match_id, white_seat
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


def test_checked_in_vectors_match_the_generator(generator: ModuleType) -> None:
    """Regenerating must be a no-op; otherwise the TS tests are stale."""
    expected = generator.render(generator.build_vectors())
    actual = generator.target_path().read_text(encoding="utf-8")
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
    keypair = keypair_from_seed(bytes.fromhex(keys["seed_hex"]))
    assert keypair.public_key_hex == keys["public_key_hex"]
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
    assert verify_domain(
        bytes.fromhex(keys["peer_public_key_hex"]),
        Domain.RESULT,
        result["payload"],
        bytes.fromhex(result["signature_hex"]),
    )
