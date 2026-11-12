"""The canonical game hash: stable over metadata, sensitive to everything else."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any

import chess
import pytest

from sccp.chess.pgn import (
    GameResult,
    Termination,
    canonical_game_hash,
    outcome_result,
    validate_pgn,
)
from sccp.config import MAINNET, TESTNET
from sccp.errors import PgnValidationError
from sccp.serialization import Domain, domain_hex

FOOLS_MATE = "1. f3 e5 2. g4 Qh4# 0-1"


def test_golden_fools_mate_hash(chess_vectors: dict[str, Any]) -> None:
    vector = chess_vectors["fools_mate"]
    assert (
        canonical_game_hash("testnet", vector["moves"], vector["result"]) == vector["testnet_hash"]
    )


def test_parsed_hash_matches_the_golden_vector(
    make_pgn: Callable[[str, Mapping[str, str]], str],
    match_tags: dict[str, str],
    chess_vectors: dict[str, Any],
) -> None:
    parsed = validate_pgn(make_pgn(FOOLS_MATE, match_tags), params=TESTNET)
    assert parsed.game_hash == chess_vectors["fools_mate"]["testnet_hash"]


def test_headers_and_comments_do_not_change_the_hash(
    make_pgn: Callable[[str, Mapping[str, str]], str], match_tags: dict[str, str]
) -> None:
    plain = validate_pgn(make_pgn(FOOLS_MATE, match_tags), params=TESTNET)

    noisy_tags = {
        **match_tags,
        "Event": "a different event",
        "Site": "elsewhere",
        "Date": "1999.12.31",
        "Round": "42",
    }
    noisy = validate_pgn(
        make_pgn("1. f3 e5 2. g4 Qh4# {[%clk 0:02:30]} 0-1", noisy_tags), params=TESTNET
    )
    assert noisy.game_hash == plain.game_hash
    assert noisy.tags["Event"] == "a different event"


def test_a_different_move_order_hashes_differently(
    make_pgn: Callable[[str, Mapping[str, str]], str], match_tags: dict[str, str]
) -> None:
    tags = {**match_tags, "Result": "0-1", "Termination": "resignation"}
    first = validate_pgn(make_pgn("1. e4 e5 0-1", tags), params=TESTNET)
    second = validate_pgn(make_pgn("1. e4 c5 0-1", tags), params=TESTNET)
    assert first.game_hash != second.game_hash


def test_result_and_network_are_part_of_the_hash() -> None:
    moves = ("f3", "e5", "g4", "Qh4#")
    base = canonical_game_hash("testnet", moves, "0-1")
    assert base != canonical_game_hash("testnet", moves, "1-0")
    assert base != canonical_game_hash("mainnet", moves, "0-1")
    assert base != canonical_game_hash(
        "testnet", moves, "0-1", start_fen="4k3/8/8/8/8/8/8/4K3 w - - 0 1"
    )


def test_game_domain_is_used() -> None:
    moves = ["f3"]
    payload = {
        "network": "testnet",
        "start_fen": chess.STARTING_FEN,
        "san_list": moves,
        "result": "0-1",
    }
    assert canonical_game_hash("testnet", moves, "0-1") == domain_hex(Domain.GAME, payload)
    assert canonical_game_hash("testnet", moves, "0-1") != domain_hex(Domain.PROOF, payload)


def test_hash_is_lowercase_hex() -> None:
    digest = canonical_game_hash("testnet", ("e4",), "1-0")
    assert re.fullmatch(r"[0-9a-f]{64}", digest)


def test_game_result_parsing() -> None:
    assert GameResult.parse("1-0") is GameResult.WHITE
    assert GameResult.parse("0-1") is GameResult.BLACK
    assert GameResult.parse("1/2-1/2") is GameResult.DRAW
    assert GameResult.parse("*") is GameResult.UNFINISHED
    assert GameResult.WHITE.is_decisive
    assert not GameResult.DRAW.is_decisive
    assert not GameResult.UNFINISHED.is_decisive
    with pytest.raises(PgnValidationError, match="invalid PGN Result"):
        GameResult.parse("white wins")


def test_outcome_result_maps_every_automatic_outcome() -> None:
    mate = chess.Board()
    for move in ("f2f3", "e7e5", "g2g4", "d8h4"):
        mate.push(chess.Move.from_uci(move))
    outcome = mate.outcome(claim_draw=False)
    assert outcome is not None
    assert outcome_result(outcome) == (GameResult.BLACK, Termination.CHECKMATE)

    stalemate = chess.Board("7k/5Q2/6K1/8/8/8/8/8 b - - 0 1").outcome(claim_draw=False)
    assert stalemate is not None
    assert outcome_result(stalemate) == (GameResult.DRAW, Termination.DRAW)

    material = chess.Board("8/8/8/8/8/8/5k2/K7 w - - 0 1").outcome(claim_draw=False)
    assert material is not None
    assert outcome_result(material) == (GameResult.DRAW, Termination.DRAW)


def test_mainnet_params_change_the_hash(
    make_pgn: Callable[[str, Mapping[str, str]], str], match_tags: dict[str, str]
) -> None:
    pgn = make_pgn(FOOLS_MATE, match_tags)
    assert (
        validate_pgn(pgn, params=TESTNET).game_hash != validate_pgn(pgn, params=MAINNET).game_hash
    )
