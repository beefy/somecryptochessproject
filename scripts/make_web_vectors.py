"""Write the protocol vectors the web UI's TypeScript tests are pinned to.

Run this only when a consensus-visible rule changes, or when a vector is added::

    python scripts/make_web_vectors.py

``src/web`` re-implements canonical JSON, bech32, Ed25519 signing, match ids,
colour draws and the game hash in TypeScript. Those ports must agree with the
Python implementation byte for byte, so the numbers they are tested against are
generated here from ``sccp`` itself rather than typed in by hand.

``tests/unit/test_web_vectors.py`` re-checks the same file against the library,
so a drift in either direction fails the Python suite as well.
"""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

import chess

from sccp.chain.tx import sign_transaction, txid
from sccp.chess.openings import books_from_document
from sccp.chess.pgn import GameResult, canonical_game_hash
from sccp.config import DEVNET
from sccp.crypto.address import encode_address
from sccp.crypto.keys import keypair_from_seed, sign_domain
from sccp.protocol.beacon import commitment, compute_beacon
from sccp.protocol.clocks import move_payload
from sccp.protocol.models import (
    Availability,
    BeaconReveal,
    SignedMove,
    Transaction,
    TxType,
)
from sccp.protocol.pairing import RosterEntry, match_id, pair, white_seat
from sccp.protocol.transcript import (
    build_win_proof,
    certificate_payload,
    create_certificate,
    create_result,
    render_transcript_pgn,
    result_payload,
    sign_certificate,
    sign_result,
    validate_pgn,
)
from sccp.serialization import Domain, canonical_json, domain_hex

NETWORK = "testnet"

# Deliberately awkward: keys that only sort correctly by code point, an NFC-
# colliding key (U+0065 U+0301 vs U+00E9), astral characters, escapes and both
# ends of the consensus integer range.
CANONICAL_PAYLOAD: dict[str, object] = {
    "zz": 1,
    "a": {"b": [1, -2, 0], "A": "\u00e9", "nested": {"deep": True}},
    "astral": "\U0001d11e clef",
    "emoji": "\U0001f600",
    "esc": 'tab\t newline\n quote" back\\slash /slash',
    "cafe\u0301": "combining key normalizes to NFC",
    "\ue000": False,
    "\U0001f600 key": "astral sorts after BMP",
    "big": 9_007_199_254_740_991,
    "neg": -9_007_199_254_740_991,
    "null": None,
}

# JavaScript's `number` cannot hold these exactly, so they are a separate vector
# that the TypeScript test builds with BigInt literals.
BIGINT_PAYLOAD: dict[str, object] = {"big": 2**64 - 1, "neg": -(2**63)}

FOOLS_MATE = ("f3", "e5", "g4", "Qh4#")

TEST_BOOK = {
    "format": 1,
    "books": {
        DEVNET.book_version: {
            "lines": [{"name": "Test Italian", "moves": ["e4", "e5", "Bc4", "Bc5"]}]
        }
    },
}
"""A one-line book, so the beacon-selected opening is always the mate prefix."""


def _devnet_accounts() -> dict[str, tuple[bytes, str]]:
    """Two fixed seats and their bech32 addresses on devnet."""
    accounts = {}
    for name, seed in (("alice", bytes([1]) * 32), ("bob", bytes([2]) * 32)):
        keypair = keypair_from_seed(seed)
        accounts[name] = (seed, encode_address(keypair.public_key_bytes, DEVNET))
    return accounts


def build_match_vectors() -> dict[str, object]:
    """A whole devnet match: pairing, certificate, scholar's mate, co-signed proof.

    The browser's proof builder is pinned to this: the certificate is what a
    relay hands a seat, and the proof is what ``build_win_proof`` produces from
    the transcript both seats signed. Nothing here is test-only — it is the
    same call sequence ``sccp devnet up`` uses.
    """
    params = DEVNET.model_copy(update={"min_plies": 1})
    book = books_from_document(TEST_BOOK)[params.book_version]
    keys = {name: keypair_from_seed(seed) for name, (seed, _) in _devnet_accounts().items()}
    addresses = {name: address for name, (_, address) in _devnet_accounts().items()}
    nonces = {"alice": "11" * 32, "bob": "22" * 32}

    availability = {
        name: Availability(
            network=params.network_id,
            miner=addresses[name],
            epoch=3,
            max_games=3,
            commitment=commitment(addresses[name], 3, nonces[name]),
        )
        for name in keys
    }
    beacon = compute_beacon(
        network=params.network_id,
        epoch=3,
        block_hash="ab" * 32,
        reveals=[
            BeaconReveal(miner=entry.miner, commitment=entry.commitment, nonce=nonces[name])
            for name, entry in availability.items()
        ],
    )
    roster = {addresses[name]: RosterEntry(address=addresses[name], max_games=3) for name in keys}
    matchups = pair(
        list(roster.values()),
        bytes.fromhex(beacon.hash),
        network=params.network_id,
        epoch=beacon.epoch,
        round_number=0,
        rounds_per_epoch=params.rounds_per_epoch,
    )
    matchup = matchups[0]
    opening = book.select(bytes.fromhex(beacon.hash), matchup.match_id)
    engines = {
        address: f"web-v1:{index:016d}" for index, address in enumerate(sorted(roster), start=1)
    }
    certificate = create_certificate(
        matchup,
        opening,
        engine_white=engines[matchup.white],
        engine_black=engines[matchup.black],
    )
    owner_of = {
        seat: ("alice" if addresses["alice"] == getattr(certificate, seat) else "bob")
        for seat in ("white", "black")
    }
    for seat in ("white", "black"):
        certificate = sign_certificate(certificate, seat, keys[owner_of[seat]])

    board = chess.Board()
    for san in certificate.opening.moves:
        board.push(board.parse_san(san))
    for san in ("Qh5", "Nf6", "Qxf7#"):
        board.push(board.parse_san(san))
    pgn = render_transcript_pgn(certificate, board, result="1-0", termination="checkmate")
    parsed = validate_pgn(pgn, params=params, require_win=True)
    result = create_result(certificate, parsed)
    for seat in ("white", "black"):
        result = sign_result(result, certificate, seat, keys[owner_of[seat]])
    proof = build_win_proof(certificate, pgn, result.result_signatures, params=params)

    return {
        "params": {
            "network_id": params.network_id,
            "rounds_per_epoch": params.rounds_per_epoch,
            "min_plies": params.min_plies,
            "quality_mode": params.quality_mode.value,
        },
        "beacon": {
            "network": beacon.network,
            "epoch": beacon.epoch,
            "hash": beacon.hash,
        },
        "seats": {
            seat: {
                "address": getattr(certificate, seat),
                "seed_hex": keys[owner_of[seat]].seed.hex(),
                "public_key_hex": keys[owner_of[seat]].public_key_hex,
            }
            for seat in ("white", "black")
        },
        "public_keys": {account: keys[name].public_key_hex for name, account in addresses.items()},
        "san_moves": list(parsed.san_moves),
        "pgn": pgn,
        "certificate": certificate.model_dump(mode="json"),
        "certificate_payload": certificate_payload(certificate),
        "result": result.model_dump(mode="json"),
        "result_payload": result_payload(result),
        "proof": proof.model_dump(mode="json"),
    }


def build_ledger_vectors() -> dict[str, object]:
    """A signed ``register`` transaction and an availability commitment."""
    keypair = keypair_from_seed(bytes([7]) * 32)
    sender = encode_address(keypair.public_key_bytes, DEVNET)
    unsigned = Transaction(
        network=DEVNET.network_id,
        type=TxType.REGISTER,
        sender=sender,
        nonce=0,
        fee=DEVNET.min_fee,
        payload={
            "public_key": keypair.public_key_hex,
            "endpoint": "http://127.0.0.1:8647",
            "engine": "sccp-web/hand-play-1",
            "bond": DEVNET.min_bond,
        },
    )
    signed = sign_transaction(keypair, unsigned)
    return {
        "seed_hex": keypair.seed.hex(),
        "register": {
            "transaction": signed.model_dump(mode="json", by_alias=True),
            "txid": txid(signed),
        },
        "availability_commitment": {
            "address": sender,
            "epoch": 3,
            "nonce": "11" * 32,
            "commitment": commitment(sender, 3, "11" * 32),
        },
    }


def build_vectors() -> dict[str, object]:
    """Return the vector document the web UI's TypeScript tests are pinned to."""
    white_key = keypair_from_seed(bytes(range(32)))
    black_key = keypair_from_seed(bytes(range(1, 33)))
    white_address = encode_address(white_key.public_key_bytes, NETWORK)
    black_address = encode_address(black_key.public_key_bytes, NETWORK)

    match = match_id(NETWORK, 3, 1, [black_address, white_address])
    beacon = sha256(b"sccp-web/beacon\x00room-1").digest()
    colour_seat = white_seat(beacon, match, [white_address, black_address])
    game_hash = canonical_game_hash(NETWORK, FOOLS_MATE, GameResult.BLACK)

    move = SignedMove(
        network=NETWORK,
        match_id=match,
        seat=white_address,
        ply=1,
        san="f3",
        signature="00" * 64,
    )
    result_fields = {
        "network": NETWORK,
        "match_id": match,
        "game_hash": game_hash,
        "result": "0-1",
        "termination": "checkmate",
        "plies": len(FOOLS_MATE),
    }

    vectors = {
        "description": (
            "Generated by scripts/make_web_vectors.py from the Python implementation. "
            "Do not edit: src/web/src/lib/*.test.ts compares its ports against these."
        ),
        "network": NETWORK,
        "canonical": {
            "payload": CANONICAL_PAYLOAD,
            "json": canonical_json(CANONICAL_PAYLOAD).decode("utf-8"),
            "game_digest": domain_hex(Domain.GAME, CANONICAL_PAYLOAD),
        },
        "bigint": {
            "json": canonical_json(BIGINT_PAYLOAD).decode("utf-8"),
            "game_digest": domain_hex(Domain.GAME, BIGINT_PAYLOAD),
        },
        "keys": {
            "seed_hex": white_key.seed.hex(),
            "public_key_hex": white_key.public_key_hex,
            "address_testnet": white_address,
            "address_mainnet": encode_address(white_key.public_key_bytes, "mainnet"),
            "peer_public_key_hex": black_key.public_key_hex,
            "peer_address_testnet": black_address,
        },
        "pairing": {
            "epoch": 3,
            "round": 1,
            "seats": [black_address, white_address],
            "match_id": match,
            "beacon_hex": beacon.hex(),
            "white_seat": colour_seat,
        },
        "move": {
            "payload": move_payload(move),
            "signature_hex": sign_domain(white_key, Domain.MOVE, move_payload(move)).hex(),
        },
        "game": {
            "san_moves": list(FOOLS_MATE),
            "result": "0-1",
            "game_hash": game_hash,
        },
        "result": {
            "payload": result_fields,
            "signature_hex": sign_domain(black_key, Domain.RESULT, result_fields).hex(),
        },
        "ledger": build_ledger_vectors(),
        "match": build_match_vectors(),
    }

    return vectors


def target_path() -> Path:
    """The checked-in location of the vector document."""
    return Path(__file__).resolve().parents[1] / "src" / "web" / "src" / "lib" / "vectors.json"


def render(vectors: dict[str, object]) -> str:
    """Serialize ``vectors`` in the exact form the checked-in file uses."""
    return json.dumps(vectors, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def main() -> None:
    """Write ``src/web/src/lib/vectors.json`` from the current library."""
    payload = render(build_vectors())
    target = target_path()
    target.write_text(payload, encoding="utf-8")
    print(f"wrote {target} ({len(payload)} bytes)")


if __name__ == "__main__":
    main()
