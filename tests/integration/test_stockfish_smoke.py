"""Real-Stockfish smoke test (``-m engine``; skipped without a binary).

Point ``SCCP_STOCKFISH`` at a binary, or run
``python scripts/fetch_engine.py --dest ./bin`` and this test finds ``./bin``.
CI installs a pinned build in a separate nightly job, so the default test run
never downloads or executes an engine.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import chess.engine
import pytest

from sccp.chess.engines import Engine, UciEngine
from sccp.chess.gameplay import play_game
from sccp.chess.pgn import validate_pgn
from sccp.config import TESTNET, NetworkParams

pytestmark = pytest.mark.engine

REPO_ROOT = Path(__file__).resolve().parents[2]


def _binary() -> Path | None:
    override = os.environ.get("SCCP_STOCKFISH")
    if override:
        path = Path(override).expanduser()
        return path if path.is_file() else None
    for candidate in sorted((REPO_ROOT / "bin").glob("stockfish*")):
        if candidate.is_file():
            return candidate
    return None


@pytest.fixture()
def engines() -> Iterator[tuple[Engine, Engine]]:
    binary = _binary()
    if binary is None:
        pytest.skip("no Stockfish binary; set SCCP_STOCKFISH or run scripts/fetch_engine.py")
    white = UciEngine.popen(binary, name="stockfish-white")
    black = UciEngine.popen(binary, name="stockfish-black")
    try:
        yield white, black
    finally:
        white.close()
        black.close()


def test_stockfish_game_validates(engines: tuple[Engine, Engine]) -> None:
    white, black = engines
    params = NetworkParams.model_validate(
        {**TESTNET.model_dump(), "max_plies": 300, "min_plies": 1}
    )
    headers = {
        "MatchID": "smoke",
        "Epoch": "2",
        "RoundNumber": "0",
        "SeatWhite": "sccpt1white",
        "SeatBlack": "sccpt1black",
        "OpeningID": "0",
        "OpeningHash": "00" * 32,
    }
    game = play_game(
        white,
        black,
        params=params,
        limit=chess.engine.Limit(nodes=50_000),
        headers=headers,
    )

    assert game.plies > 0
    assert game.plies <= params.max_plies
    parsed = validate_pgn(game.pgn, params=params)
    assert parsed.san_moves == game.san_moves
