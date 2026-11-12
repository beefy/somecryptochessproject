"""Shared fixtures for the sccp test suite."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import chess
import pytest

from sccp.chess.openings import OpeningBook, books_from_document
from sccp.chess.pgn import GameExpectations, ParsedGame, validate_pgn
from sccp.config import DEVNET, NetworkParams
from sccp.crypto.address import encode_address
from sccp.crypto.keys import Keypair, keypair_from_seed
from sccp.crypto.wallet import FAST_KDF, KdfParams
from sccp.protocol.beacon import commitment, compute_beacon
from sccp.protocol.models import (
    Availability,
    Beacon,
    BeaconReveal,
    MatchCertificate,
    Matchup,
    WinProof,
)
from sccp.protocol.models import (
    GameResult as ProtocolGameResult,
)
from sccp.protocol.pairing import RosterEntry, pair
from sccp.protocol.transcript import (
    build_win_proof,
    create_certificate,
    create_result,
    render_transcript_pgn,
    sign_certificate,
    sign_result,
)
from sccp.protocol.verification import ProofContext

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def vectors() -> dict[str, Any]:
    """Golden vectors from tests/fixtures/crypto_vectors.json."""
    data: object = json.loads((FIXTURES / "crypto_vectors.json").read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


@pytest.fixture(scope="session")
def chess_vectors() -> dict[str, Any]:
    """Golden vectors from tests/fixtures/chess_vectors.json."""
    data: object = json.loads((FIXTURES / "chess_vectors.json").read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


@pytest.fixture()
def fast_kdf() -> KdfParams:
    """Cheap scrypt parameters; production keyfiles use n=2**15."""
    return FAST_KDF


@pytest.fixture()
def passphrase() -> str:
    """A passphrase with spaces and mixed case."""
    return "correct horse battery staple"


@pytest.fixture()
def match_tags(chess_vectors: dict[str, Any]) -> dict[str, str]:
    """A complete, self-consistent set of sccp PGN headers."""
    return {
        "Event": "sccp match",
        "Site": "?",
        "Date": "2026.01.01",
        "Round": "1",
        "White": "sccpt1seatwhite",
        "Black": "sccpt1seatblack",
        "Result": "0-1",
        "Termination": "checkmate",
        "MatchID": "m-1",
        "Epoch": "2",
        "RoundNumber": "0",
        "SeatWhite": "sccpt1seatwhite",
        "SeatBlack": "sccpt1seatblack",
        "EngineWhite": "mock-v1:1111111111111111",
        "EngineBlack": "mock-v1:2222222222222222",
        "OpeningID": "0",
        "OpeningHash": chess_vectors["opening"]["hash"],
        "TimeControl": "1800+0",
        "Rules": "chess",
    }


@pytest.fixture()
def make_pgn() -> Callable[[str, Mapping[str, str]], str]:
    """Render a PGN with tags, as a helper factory."""

    def build(moves: str, tags: Mapping[str, str]) -> str:
        headers = "".join(f'[{tag} "{value}"]\n' for tag, value in tags.items())
        return f"{headers}\n{moves}\n"

    return build


@pytest.fixture()
def expectations(match_tags: Mapping[str, str]) -> GameExpectations:
    """The certificate expectations matching :func:`match_tags`."""
    return GameExpectations(
        match_id=match_tags["MatchID"],
        epoch=int(match_tags["Epoch"]),
        round_number=int(match_tags["RoundNumber"]),
        seat_white=match_tags["SeatWhite"],
        seat_black=match_tags["SeatBlack"],
        engine_white=match_tags["EngineWhite"],
        engine_black=match_tags["EngineBlack"],
        opening_id=int(match_tags["OpeningID"]),
        opening_hash=match_tags["OpeningHash"],
        time_control=match_tags["TimeControl"],
    )


# --- Matchmaking fixtures ----------------------------------------------------


@dataclass(frozen=True)
class ProtocolAccount:
    """A deterministic keypair plus its testnet address."""

    keypair: Keypair
    address: str


@dataclass(frozen=True)
class SignedMatch:
    """A complete, verifiable match produced without the relay."""

    params: NetworkParams
    accounts: Mapping[str, ProtocolAccount]
    book: OpeningBook
    beacon: Beacon
    availability: Mapping[str, Availability]
    roster: Mapping[str, RosterEntry]
    public_keys: Mapping[str, str]
    matchup: Matchup
    certificate: MatchCertificate
    pgn: str
    parsed: ParsedGame
    result: ProtocolGameResult
    proof: WinProof
    context: ProofContext


@pytest.fixture()
def protocol_params() -> NetworkParams:
    """Devnet parameters with a one-ply proof threshold."""
    return DEVNET.model_copy(update={"min_plies": 1})


@pytest.fixture()
def protocol_book(protocol_params: NetworkParams) -> OpeningBook:
    """A one-line book, so beacon selection is always the mate prefix."""
    books = books_from_document(
        {
            "format": 1,
            "books": {
                protocol_params.book_version: {
                    "lines": [
                        {
                            "name": "Test Italian",
                            "moves": ["e4", "e5", "Bc4", "Bc5"],
                        }
                    ]
                }
            },
        }
    )
    return books[protocol_params.book_version]


@pytest.fixture()
def protocol_accounts(protocol_params: NetworkParams) -> dict[str, ProtocolAccount]:
    """Alice, Bob and Carol, derived from fixed seeds."""
    accounts: dict[str, ProtocolAccount] = {}
    for index, name in enumerate(("alice", "bob", "carol"), start=1):
        keypair = keypair_from_seed(bytes([index]) * 32)
        accounts[name] = ProtocolAccount(
            keypair=keypair,
            address=encode_address(keypair.public_key_bytes, protocol_params),
        )
    return accounts


@pytest.fixture()
def protocol_availability(
    protocol_params: NetworkParams, protocol_accounts: Mapping[str, ProtocolAccount]
) -> dict[str, Availability]:
    """Availability commits for Alice and Bob in epoch 3."""
    nonces = {"alice": "11" * 32, "bob": "22" * 32}
    return {
        name: Availability(
            network=protocol_params.network_id,
            miner=protocol_accounts[name].address,
            epoch=3,
            max_games=3,
            commitment=commitment(protocol_accounts[name].address, 3, nonce),
        )
        for name, nonce in nonces.items()
    }


@pytest.fixture()
def protocol_beacon(
    protocol_params: NetworkParams, protocol_availability: Mapping[str, Availability]
) -> Beacon:
    """A beacon with Alice's and Bob's reveals, no drand mix."""
    return compute_beacon(
        network=protocol_params.network_id,
        epoch=3,
        block_hash="ab" * 32,
        reveals=[
            BeaconReveal(
                miner=availability.miner,
                commitment=availability.commitment,
                nonce=nonce,
            )
            for availability, nonce in zip(
                protocol_availability.values(),
                ("11" * 32, "22" * 32),
                strict=True,
            )
        ],
    )


@pytest.fixture()
def protocol_roster(
    protocol_accounts: Mapping[str, ProtocolAccount],
) -> dict[str, RosterEntry]:
    """Alice and Bob, three games each."""
    return {
        protocol_accounts[name].address: RosterEntry(
            address=protocol_accounts[name].address, max_games=3
        )
        for name in ("alice", "bob")
    }


@pytest.fixture()
def protocol_public_keys(
    protocol_accounts: Mapping[str, ProtocolAccount],
) -> dict[str, str]:
    """Registered public keys for every test account."""
    return {
        account.address: account.keypair.public_key_hex for account in protocol_accounts.values()
    }


@pytest.fixture()
def protocol_matchup(
    protocol_params: NetworkParams,
    protocol_beacon: Beacon,
    protocol_roster: Mapping[str, RosterEntry],
) -> Matchup:
    """The single pairing for epoch 3, round 0."""
    matchups = pair(
        list(protocol_roster.values()),
        bytes.fromhex(protocol_beacon.hash),
        network=protocol_params.network_id,
        epoch=protocol_beacon.epoch,
        round_number=0,
        rounds_per_epoch=protocol_params.rounds_per_epoch,
    )
    assert len(matchups) == 1
    return matchups[0]


@pytest.fixture()
def protocol_certificate(
    protocol_accounts: Mapping[str, ProtocolAccount],
    protocol_book: OpeningBook,
    protocol_beacon: Beacon,
    protocol_matchup: Matchup,
) -> MatchCertificate:
    """A certificate signed by both seats."""
    opening = protocol_book.select(bytes.fromhex(protocol_beacon.hash), protocol_matchup.match_id)
    engines = {
        account.address: f"mock-v1:{index:016d}"
        for index, account in enumerate(protocol_accounts.values(), start=1)
    }
    certificate = create_certificate(
        protocol_matchup,
        opening,
        engine_white=engines[protocol_matchup.white],
        engine_black=engines[protocol_matchup.black],
    )
    by_address = {account.address: account for account in protocol_accounts.values()}
    certificate = sign_certificate(certificate, "white", by_address[certificate.white].keypair)
    return sign_certificate(certificate, "black", by_address[certificate.black].keypair)


def build_signed_match(
    *,
    params: NetworkParams,
    certificate_accounts: Mapping[str, ProtocolAccount],
    book: OpeningBook,
    beacon: Beacon,
    roster: Mapping[str, RosterEntry],
    public_keys: Mapping[str, str],
    availability: Mapping[str, str],
    round_number: int = 0,
) -> SignedMatch:
    """Pair, certify, play scholar's mate and build the proof.

    ``roster`` must already be the roster for ``beacon.epoch``: the pairing is
    recomputed from the beacon seed, so the certificate is exactly what a chain
    deriving its own beacon would produce.
    """
    matchups = pair(
        list(roster.values()),
        bytes.fromhex(beacon.hash),
        network=params.network_id,
        epoch=beacon.epoch,
        round_number=round_number,
        rounds_per_epoch=params.rounds_per_epoch,
    )
    assert len(matchups) == 1, "the test rosters pair exactly once"
    matchup = matchups[0]
    opening = book.select(bytes.fromhex(beacon.hash), matchup.match_id)
    engines = {
        account.address: f"mock-v1:{index:016d}"
        for index, account in enumerate(certificate_accounts.values(), start=1)
    }
    certificate = create_certificate(
        matchup,
        opening,
        engine_white=engines[matchup.white],
        engine_black=engines[matchup.black],
    )
    by_address = {account.address: account for account in certificate_accounts.values()}
    certificate = sign_certificate(certificate, "white", by_address[certificate.white].keypair)
    certificate = sign_certificate(certificate, "black", by_address[certificate.black].keypair)

    board = chess.Board()
    for san in certificate.opening.moves:
        board.push(board.parse_san(san))
    for san in ("Qh5", "Nf6", "Qxf7#"):
        board.push(board.parse_san(san))
    pgn = render_transcript_pgn(certificate, board, result="1-0", termination="checkmate")
    parsed = validate_pgn(pgn, params=params, require_win=True)
    result = create_result(certificate, parsed)
    for seat in ("white", "black"):
        result = sign_result(
            result, certificate, seat, by_address[getattr(certificate, seat)].keypair
        )
    proof = build_win_proof(certificate, pgn, result.result_signatures, params=params)
    context = ProofContext(
        params=params,
        beacon=beacon,
        roster=roster,
        public_keys=public_keys,
        book=book,
        availability=dict(availability),
    )
    return SignedMatch(
        params=params,
        accounts=certificate_accounts,
        book=book,
        beacon=beacon,
        availability={},
        roster=roster,
        public_keys=public_keys,
        matchup=matchup,
        certificate=certificate,
        pgn=pgn,
        parsed=parsed,
        result=result,
        proof=proof,
        context=context,
    )


@pytest.fixture()
def signed_match(
    protocol_params: NetworkParams,
    protocol_accounts: Mapping[str, ProtocolAccount],
    protocol_book: OpeningBook,
    protocol_beacon: Beacon,
    protocol_availability: Mapping[str, Availability],
    protocol_roster: Mapping[str, RosterEntry],
    protocol_public_keys: Mapping[str, str],
) -> SignedMatch:
    """A full scholar's-mate proof and the context that verifies it."""
    match = build_signed_match(
        params=protocol_params,
        certificate_accounts=protocol_accounts,
        book=protocol_book,
        beacon=protocol_beacon,
        roster=protocol_roster,
        public_keys=protocol_public_keys,
        availability={
            availability.miner: availability.commitment
            for availability in protocol_availability.values()
        },
    )
    return replace(match, availability=protocol_availability)


# --- Ledger fixtures ---------------------------------------------------------

GENESIS_DIR = Path(__file__).resolve().parent.parent / "genesis"


@pytest.fixture()
def devnet_chain(protocol_params: NetworkParams, protocol_book: OpeningBook) -> Any:
    """A chain at the pinned devnet genesis, with the one-line test book."""
    from sccp.chain.chain import Chain
    from sccp.chain.genesis import load_genesis

    block, state = load_genesis(GENESIS_DIR / "devnet.json", protocol_params)
    return Chain(
        params=protocol_params,
        genesis_block=block,
        genesis_state=state,
        book=protocol_book,
    )


@pytest.fixture()
def miner_keys(protocol_accounts: Mapping[str, ProtocolAccount]) -> dict[str, Keypair]:
    """Address -> keypair, for builders and certificate signing."""
    return {account.address: account.keypair for account in protocol_accounts.values()}


@pytest.fixture()
def mine_block(protocol_params: NetworkParams) -> Callable[..., Any]:
    """Build and connect blocks with the real builder.

    ``mine(chain, keypair, mempool=None, txs=(), proofs=(), timestamp=None)``
    admits whatever it is given, builds, connects and returns the block.
    Timestamps default to a per-test clock above the devnet genesis.
    """

    from sccp.chain.mempool import Mempool
    from sccp.miner.block_builder import BlockBuilder

    clock = {"time": protocol_params.genesis_timestamp or 1_700_000_000}

    def mine(
        chain: Any,
        keypair: Keypair,
        *,
        mempool: Any = None,
        txs: Sequence[Any] = (),
        proofs: Sequence[Any] = (),
        timestamp: int | None = None,
    ) -> Any:
        pool = (
            mempool
            if mempool is not None
            else Mempool(params=chain.params, tip_state=chain.state, book=chain.book)
        )
        for tx in txs:
            pool.add_transaction(tx)
        for proof in proofs:
            pool.add_proof(proof)
        stamp = clock["time"] + 1 if timestamp is None else timestamp
        clock["time"] = max(clock["time"], stamp)
        builder = BlockBuilder(params=chain.params, chain=chain, mempool=pool, keypair=keypair)
        block = builder.build(timestamp=stamp)
        assert block is not None, "the builder refused to produce a block"
        chain.add_block(block)
        return block

    return mine


@pytest.fixture()
def chain_match(
    protocol_accounts: Mapping[str, ProtocolAccount],
) -> Callable[..., SignedMatch]:
    """Build a provable match against a chain's own epoch record."""

    def build(chain: Any, epoch: int | None = None, round_number: int = 0) -> SignedMatch:
        state = chain.state
        target = state.current_epoch() - 1 if epoch is None else epoch
        record = state.epochs.get(target)
        assert record is not None and record.matchable and record.beacon is not None
        roster = {
            address: RosterEntry(address=address, max_games=member.max_games)
            for address, member in record.roster.items()
        }
        return build_signed_match(
            params=chain.params,
            certificate_accounts=protocol_accounts,
            book=chain.book,
            beacon=record.beacon,
            roster=roster,
            public_keys=state.public_keys(),
            availability=dict(record.availability),
            round_number=round_number,
        )

    return build


@dataclass(frozen=True, slots=True)
class LiveChain:
    """A bootstrapped devnet chain plus the keys that produced it."""

    chain: Any
    faucet: Keypair
    faucet_address: str
    accounts: Mapping[str, ProtocolAccount]
    nonce_secrets: Mapping[str, str]


@pytest.fixture()
def live_chain(
    mine_block: Callable[..., Any],
    devnet_chain: Any,
    protocol_accounts: Mapping[str, ProtocolAccount],
) -> LiveChain:
    """Survive the bootstrap window: fund, register and ready a two-seat roster.

    The chain ends at height 3 with a matchable epoch 3, which is the earliest
    epoch a fresh network can pair in (availability for epoch 2 could never be
    committed). ``mine_block``'s clock keeps running for the test afterwards.
    """
    from sccp.chain.genesis import faucet_keypair
    from sccp.chain.mempool import Mempool
    from sccp.crypto.address import encode_address
    from sccp.wallet.service import WalletService

    chain = devnet_chain
    params = chain.params
    faucet = faucet_keypair("devnet")
    faucet_address = encode_address(faucet.public_key_bytes, params)
    miner = protocol_accounts["carol"].keypair
    secrets = {"alice": "11" * 32, "bob": "22" * 32}
    funding = 100 * 100_000_000

    pool = Mempool(params=params, tip_state=chain.state, book=chain.book)
    for name in ("alice", "bob"):
        pool.add_transaction(
            WalletService(params, pool.projection).build_transfer(
                faucet, protocol_accounts[name].address, funding
            )
        )
    for name in ("alice", "bob"):
        pool.add_transaction(
            WalletService(params, pool.projection).build_register(
                protocol_accounts[name].keypair,
                endpoint=f"tcp://{name}.test",
                engine="mock-v1",
            )
        )
    for name in ("alice", "bob"):
        pool.add_transaction(
            WalletService(params, pool.projection).build_availability(
                protocol_accounts[name].keypair,
                nonce_secret=secrets[name],
                max_games=params.rounds_per_epoch,
            )
        )
    mine_block(chain, miner, mempool=pool)

    pool = Mempool(params=params, tip_state=chain.state, book=chain.book)
    for name in ("alice", "bob"):
        pool.add_transaction(
            WalletService(params, pool.projection).build_reveal(
                protocol_accounts[name].keypair, secrets[name]
            )
        )
    mine_block(chain, miner, mempool=pool)
    mine_block(chain, miner)
    return LiveChain(
        chain=chain,
        faucet=faucet,
        faucet_address=faucet_address,
        accounts=protocol_accounts,
        nonce_secrets=secrets,
    )
