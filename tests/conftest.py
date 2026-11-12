"""Shared fixtures for the sccp test suite."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest

from sccp.chess.pgn import GameExpectations
from sccp.crypto.wallet import FAST_KDF, KdfParams

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
    )
