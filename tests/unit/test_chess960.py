"""Chess960: rules, start positions, pairing and the C960 reward asset."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import chess
import pytest

from sccp.chain.block import minted_subsidy, minted_variant_rewards
from sccp.chain.mempool import Mempool
from sccp.chain.state import EpochRecord, State, epoch_end_height
from sccp.chain.tx import apply_transaction, validate_transaction
from sccp.chess.engines import MockEngine
from sccp.chess.gameplay import play_game
from sccp.chess.pgn import GameExpectations, PgnValidationError, validate_pgn
from sccp.chess.variants import (
    board_for_variant,
    chess960_start_fen,
    normalise_start_fen,
    select_chess960_start,
)
from sccp.config import (
    CHESS960_ASSET_ID,
    CHESS960_VARIANT,
    DEVNET,
    STANDARD_VARIANT,
    NetworkParams,
)
from sccp.crypto.address import encode_address
from sccp.crypto.pq import hybrid_keypair_from_seed
from sccp.errors import StateError
from sccp.miner.matchmaker import Matchmaker
from sccp.protocol.beacon import commitment, compute_beacon
from sccp.protocol.models import Beacon, BeaconReveal, OpeningRef, RosterMember
from sccp.protocol.pairing import RosterEntry, match_id, pair
from sccp.protocol.transcript import (
    build_win_proof,
    create_certificate,
    create_result,
    opening_hash,
    render_transcript_pgn,
    sign_certificate,
    sign_result,
)
from sccp.serialization import domain_hex
from sccp.wallet.service import WalletService


def _headers(
    *,
    match_id: str,
    start_fen: str,
    variant: str,
    opening_id: int,
    opening_hash_value: str,
) -> dict[str, str]:
    """:func:`play_game` headers for a minimal certificate-shaped game."""
    headers = {
        "Event": "sccp match",
        "Site": "?",
        "Date": "2026.01.01",
        "Round": "0",
        "White": "sccpt1seatwhite",
        "Black": "sccpt1seatblack",
        "MatchID": match_id,
        "Epoch": "3",
        "RoundNumber": "0",
        "SeatWhite": "sccpt1seatwhite",
        "SeatBlack": "sccpt1seatblack",
        "EngineWhite": "mock-v1:white",
        "EngineBlack": "mock-v1:black",
        "OpeningID": str(opening_id),
        "OpeningHash": opening_hash_value,
        "Rules": "chess960" if variant == CHESS960_VARIANT else "chess",
    }
    if variant == CHESS960_VARIANT:
        headers["FEN"] = start_fen
        headers["SetUp"] = "1"
        headers["Variant"] = "Chess960"
    return headers


def test_chess960_pgn_replays_from_its_start_position() -> None:
    start = chess960_start_fen(0)
    hash_value = opening_hash("none", 0, (), start_fen=start, variant=CHESS960_VARIANT)
    headers = _headers(
        match_id="ab" * 32,
        start_fen=start,
        variant=CHESS960_VARIANT,
        opening_id=0,
        opening_hash_value=hash_value,
    )
    game = play_game(
        MockEngine(seed=3),
        MockEngine(seed=9),
        params=DEVNET,
        variant=CHESS960_VARIANT,
        start_fen=start,
        headers=headers,
    )
    expected = GameExpectations(
        match_id="ab" * 32,
        epoch=3,
        round_number=0,
        seat_white="sccpt1seatwhite",
        seat_black="sccpt1seatblack",
        engine_white="mock-v1:white",
        engine_black="mock-v1:black",
        opening_id=0,
        opening_hash=hash_value,
        variant=CHESS960_VARIANT,
        start_fen=start,
    )
    parsed = validate_pgn(game.pgn, params=DEVNET, expected=expected)
    assert parsed.start_fen == start
    assert parsed.san_moves == game.san_moves
    assert parsed.game_hash != domain_hex(
        "sccp/v1/game",
        {
            "network": DEVNET.network_id,
            "start_fen": chess.STARTING_FEN,
            "san_list": list(game.san_moves),
            "result": game.result.value,
        },
    )


def test_chess960_pgn_must_match_the_certificate_start() -> None:
    start = chess960_start_fen(0)
    other = chess960_start_fen(1)
    hash_value = opening_hash("none", 0, (), start_fen=start, variant=CHESS960_VARIANT)
    headers = _headers(
        match_id="cd" * 32,
        start_fen=start,
        variant=CHESS960_VARIANT,
        opening_id=0,
        opening_hash_value=hash_value,
    )
    game = play_game(
        MockEngine(seed=3),
        MockEngine(seed=9),
        params=DEVNET,
        variant=CHESS960_VARIANT,
        start_fen=start,
        headers=headers,
    )
    expected = GameExpectations(
        match_id="cd" * 32,
        epoch=3,
        round_number=0,
        seat_white="sccpt1seatwhite",
        seat_black="sccpt1seatblack",
        engine_white="mock-v1:white",
        engine_black="mock-v1:black",
        opening_id=0,
        opening_hash=hash_value,
        variant=CHESS960_VARIANT,
        start_fen=other,
    )
    with pytest.raises(PgnValidationError, match="certificate says"):
        validate_pgn(game.pgn, params=DEVNET, expected=expected)


def test_only_the_960_start_positions_are_accepted() -> None:
    assert normalise_start_fen(chess960_start_fen(959), CHESS960_VARIANT).endswith("CAca - 0 1")
    board = board_for_variant(CHESS960_VARIANT, chess960_start_fen(1))
    assert board.chess960
    assert board.ply() == 0
    with pytest.raises(Exception, match="not one of the 960"):
        normalise_start_fen("4k3/8/8/8/8/8/8/4K3 w - - 0 1", CHESS960_VARIANT)


def test_pairing_groups_by_variant() -> None:
    roster = [
        RosterEntry(address="sccpt1standarda", variant=STANDARD_VARIANT),
        RosterEntry(address="sccpt1standardb", variant=STANDARD_VARIANT),
        RosterEntry(address="sccpt1chess960a", variant=CHESS960_VARIANT),
        RosterEntry(address="sccpt1chess960b", variant=CHESS960_VARIANT),
    ]
    matchups = pair(
        roster,
        b"\x11" * 32,
        network="testnet",
        epoch=3,
        round_number=0,
        rounds_per_epoch=3,
    )
    assert len(matchups) == 2
    assert {matchup.variant for matchup in matchups} == {
        STANDARD_VARIANT,
        CHESS960_VARIANT,
    }
    by_address = {entry.address: entry.variant for entry in roster}
    for matchup in matchups:
        seats = {by_address[matchup.white], by_address[matchup.black]}
        assert seats == {matchup.variant}


def test_relay_opens_a_chess960_session_with_a_beacon_position() -> None:
    keypairs = [hybrid_keypair_from_seed(bytes([40 + index]) * 32) for index in range(4)]
    addresses = [encode_address(keypair.public_key_bytes, DEVNET) for keypair in keypairs]
    roster = [
        RosterEntry(address=addresses[0], max_games=3, variant=STANDARD_VARIANT),
        RosterEntry(address=addresses[1], max_games=3, variant=STANDARD_VARIANT),
        RosterEntry(address=addresses[2], max_games=3, variant=CHESS960_VARIANT),
        RosterEntry(address=addresses[3], max_games=3, variant=CHESS960_VARIANT),
    ]
    public_keys = {
        address: keypair.public_key_hex
        for address, keypair in zip(addresses, keypairs, strict=True)
    }
    engines = {address: "mock-v1:test" for address in addresses}
    beacon = Beacon(
        network=DEVNET.network_id,
        epoch=3,
        hash="ef" * 32,
        block_hash="12" * 32,
    )
    matchmaker = Matchmaker(
        DEVNET,
        beacon=beacon,
        roster=roster,
        public_keys=public_keys,
        engines=engines,
    )
    sessions = matchmaker.open_round(0)
    assert len(sessions) == 2
    chess960_sessions = [
        session for session in sessions if session.certificate.variant == CHESS960_VARIANT
    ]
    assert len(chess960_sessions) == 1
    certificate = chess960_sessions[0].certificate
    position = select_chess960_start(bytes.fromhex(beacon.hash), certificate.match_id)
    assert certificate.opening.line_id == position
    assert certificate.opening.start_fen == chess960_start_fen(position)
    assert certificate.opening.moves == ()
    assert certificate.opening.hash == opening_hash(
        "none",
        position,
        (),
        start_fen=certificate.opening.start_fen,
        variant=CHESS960_VARIANT,
    )


def test_chess960_proofs_mint_c960_not_sccp() -> None:
    standard = SimpleNamespace(variant=STANDARD_VARIANT)
    chess960 = SimpleNamespace(variant=CHESS960_VARIANT)
    assert minted_subsidy(DEVNET, 100, proofs=[standard], minted=0) == DEVNET.subsidy
    assert minted_subsidy(DEVNET, 100, proofs=[chess960], minted=0) == 0
    rewards = minted_variant_rewards(
        DEVNET,
        100,
        proofs=[chess960],
        minted_assets={},
    )
    assert rewards == {CHESS960_ASSET_ID: DEVNET.variant(CHESS960_VARIANT).subsidy}


def test_c960_rewards_mature_like_native_coinbase() -> None:
    state = State(params=DEVNET)
    address = "sccpt1q87tje79rytrvlasq2g4g86c09yzalk3g4hlepzkz7p95hdlemdxgwqrd9g"
    state.credit_asset_coinbase(address, CHESS960_ASSET_ID, 123, height=10)
    assert state.account(address).assets == {}
    assert state.minted_assets[CHESS960_ASSET_ID] == 123
    state.release_matured(10 + DEVNET.coinbase_maturity)
    assert state.asset_balance(address, CHESS960_ASSET_ID) == 123


def test_c960_and_native_coinbases_mature_on_their_own_schedules() -> None:
    state = State(params=DEVNET)
    address = "sccpt1q87tje79rytrvlasq2g4g86c09yzalk3g4hlepzkz7p95hdlemdxgwqrd9g"
    maturity = DEVNET.coinbase_maturity
    state.credit_coinbase(address, 700, height=10)
    state.credit_asset_coinbase(address, CHESS960_ASSET_ID, 123, height=11)
    account = state.account(address)
    assert account.balance == 0
    assert account.immature == 700
    assert account.assets == {}
    assert account.immature_assets == {CHESS960_ASSET_ID: 123}
    assert state.minted == 700
    assert state.minted_assets == {CHESS960_ASSET_ID: 123}

    assert state.release_matured(10 + maturity - 1) == 0
    assert state.release_matured(10 + maturity) == 700
    assert account.balance == 700
    assert account.immature == 0
    assert state.asset_balance(address, CHESS960_ASSET_ID) == 0
    assert account.immature_assets == {CHESS960_ASSET_ID: 123}

    # The return value is the native total only: the C960 release below moves
    # the asset balance without adding to that total.
    assert state.release_matured(11 + maturity - 1) == 0
    assert state.release_matured(11 + maturity) == 0
    assert state.asset_balance(address, CHESS960_ASSET_ID) == 123
    assert account.assets == {CHESS960_ASSET_ID: 123}
    assert account.immature_assets.get(CHESS960_ASSET_ID, 0) == 0
    assert account.asset_coinbase_queue == []


def test_credit_asset_coinbase_ignores_zero_and_rejects_negative() -> None:
    state = State(params=DEVNET)
    address = "sccpt1q87tje79rytrvlasq2g4g86c09yzalk3g4hlepzkz7p95hdlemdxgwqrd9g"
    state.credit_asset_coinbase(address, CHESS960_ASSET_ID, 0, 1)
    assert address not in state.accounts
    assert state.minted_assets == {}
    with pytest.raises(StateError):
        state.credit_asset_coinbase(address, CHESS960_ASSET_ID, -1, 1)


def test_asset_transfer_moves_c960_and_pays_the_fee_in_sccp() -> None:
    sender = hybrid_keypair_from_seed(bytes([77]) * 32)
    recipient_keypair = hybrid_keypair_from_seed(bytes([78]) * 32)
    sender_address = encode_address(sender.public_key_bytes, DEVNET)
    recipient = encode_address(recipient_keypair.public_key_bytes, DEVNET)
    state = State(params=DEVNET)
    state.ensure(sender_address).balance = DEVNET.min_fee
    state.credit_asset(sender_address, CHESS960_ASSET_ID, 500)
    service = WalletService(DEVNET, state)
    tx = service.build_transfer(
        sender,
        recipient,
        200,
        asset=CHESS960_VARIANT,
        fee=DEVNET.min_fee,
    )
    assert tx.payload["asset"] == CHESS960_ASSET_ID
    validate_transaction(tx, DEVNET, state, height=1)
    apply_transaction(state, tx, height=1)
    assert state.asset_balance(sender_address, CHESS960_ASSET_ID) == 300
    assert state.asset_balance(recipient, CHESS960_ASSET_ID) == 200
    assert state.spendable(sender_address) == 0


C960_IDENTITY_POSITION = 518
"""Position 518 is the standard array, so a C960 game can replay a known mate."""


def _identity_beacon(
    params: NetworkParams, epoch: int, seats: tuple[str, str], match: str
) -> tuple[Beacon, dict[str, str]]:
    """A beacon the chain can recompute whose draw picks the identity position.

    The beacon hash mixes the block hash with the epoch's reveals, so any block
    hash is a legal input: the search varies that free field until
    :func:`select_chess960_start` returns :data:`C960_IDENTITY_POSITION`.
    """
    nonces = {address: f"{index:02x}" * 32 for index, address in enumerate(seats, start=1)}
    availability = {address: commitment(address, epoch, nonce) for address, nonce in nonces.items()}
    reveals = [
        BeaconReveal(miner=address, commitment=digest, nonce=nonces[address])
        for address, digest in availability.items()
    ]
    for counter in range(1 << 16):
        beacon = compute_beacon(
            network=params.network_id,
            epoch=epoch,
            block_hash=f"{counter:064x}",
            reveals=reveals,
        )
        if select_chess960_start(bytes.fromhex(beacon.hash), match) == C960_IDENTITY_POSITION:
            return beacon, availability
    raise AssertionError("no block hash drew the identity position")


def test_a_chess960_proof_settles_against_a_chess960_roster(devnet_chain: Any) -> None:
    """The epoch context must carry each seat's variant, or C960 proofs fail.

    A Chess960 proof is verified against the same-variant half of the roster, so
    the roster a block (or the mempool) derives from the epoch record has to keep
    every member's variant instead of falling back to standard.
    """
    chain = devnet_chain
    params = chain.params
    epoch = 3
    by_address = {
        encode_address(keypair.public_key_bytes, params): keypair
        for keypair in (
            hybrid_keypair_from_seed(bytes([1]) * 32),
            hybrid_keypair_from_seed(bytes([2]) * 32),
        )
    }
    seats = tuple(sorted(by_address))
    match = match_id(params.network_id, epoch, 0, seats)
    beacon, availability = _identity_beacon(params, epoch, seats, match)

    roster = {
        address: RosterEntry(address=address, max_games=3, variant=CHESS960_VARIANT)
        for address in seats
    }
    matchups = pair(
        list(roster.values()),
        bytes.fromhex(beacon.hash),
        network=params.network_id,
        epoch=epoch,
        round_number=0,
        rounds_per_epoch=params.rounds_per_epoch,
    )
    assert len(matchups) == 1, "two same-variant seats pair exactly once"
    matchup = matchups[0]
    assert matchup.variant == CHESS960_VARIANT

    start_fen = chess960_start_fen(C960_IDENTITY_POSITION)
    opening = OpeningRef(
        book_version="none",
        line_id=C960_IDENTITY_POSITION,
        hash=opening_hash(
            "none",
            C960_IDENTITY_POSITION,
            (),
            start_fen=start_fen,
            variant=CHESS960_VARIANT,
        ),
        moves=(),
        start_fen=start_fen,
        variant=CHESS960_VARIANT,
    )
    engines = {address: f"mock-v1:{index:016d}" for index, address in enumerate(seats, start=1)}
    certificate = create_certificate(
        matchup,
        opening,
        engine_white=engines[matchup.white],
        engine_black=engines[matchup.black],
    )
    for seat in ("white", "black"):
        certificate = sign_certificate(certificate, seat, by_address[getattr(certificate, seat)])

    board = board_for_variant(CHESS960_VARIANT, start_fen)
    for san in ("e4", "e5", "Bc4", "Nc6", "Qh5", "Nf6", "Qxf7#"):
        board.push(board.parse_san(san))
    assert board.is_checkmate(), "the identity position takes the scholar's mate"
    pgn = render_transcript_pgn(certificate, board, result="1-0", termination="checkmate")
    parsed = validate_pgn(pgn, params=params, require_win=True)
    result = create_result(certificate, parsed)
    for seat in ("white", "black"):
        result = sign_result(result, certificate, seat, by_address[getattr(certificate, seat)])
    proof = build_win_proof(certificate, pgn, result.result_signatures, params=params)
    assert proof.variant == CHESS960_VARIANT

    state = chain.state.copy()
    state.height = epoch_end_height(epoch, params)
    for address, keypair in by_address.items():
        state.ensure(address).public_key = keypair.public_key_hex
    state.epochs[epoch] = EpochRecord(
        epoch=epoch,
        beacon=beacon,
        roster={
            address: RosterMember(
                public_key=by_address[address].public_key_hex,
                engine=engines[address],
                variant=CHESS960_VARIANT,
                max_games=3,
            )
            for address in seats
        },
        availability=availability,
        quotas={address: 3 for address in seats},
        matchable=True,
    )

    pool = Mempool(params=params, tip_state=state, book=None)
    assert pool.add_proof(proof) == proof.proof_id
    assert pool.has_proof(proof.proof_id) is True
