"""``scripts/play_miner.py``: the human seat, its solo table and its game loop.

The script lives outside the package, so these tests import it by path and drive
it headlessly: a fake ``input`` plays the person, the relay's clock is a value
the test moves, and the opponent is the real ``EngineMiner`` from the library.
What is being checked is that the script speaks the same protocol a miner does —
signed joins, signed moves, receipts and the flag path — not that it renders
nicely.
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType
from typing import Any

import chess
import pytest

from sccp.chess.engines import MockEngine
from sccp.config import DEVNET
from sccp.crypto.keys import keypair_from_seed
from sccp.protocol.clocks import FIXED_TIME_CONTROL

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "play_miner.py"
"""The script under test."""

HUMAN_SEED = bytes([7]) * 32
"""The key the tests seat at the table."""


@pytest.fixture(scope="module")
def play() -> ModuleType:
    """Import ``scripts/play_miner.py`` as a module.

    ``sys.modules`` has to know the module before it runs: it uses
    ``from __future__ import annotations``, and ``dataclasses`` resolves the
    string annotations through the module registry.
    """
    spec = importlib.util.spec_from_file_location("sccp_play_miner", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@contextmanager
def _solo(play: ModuleType, *, clock_ms: Callable[[], int], **options: Any) -> Iterator[Any]:
    """A solo table with a fake clock, closed even when an assertion fails."""
    table = play._solo_table(
        DEVNET,
        keypair_from_seed(HUMAN_SEED),
        play.Options(**{"poll_seconds": 0.01, **options}),
        clock_ms=clock_ms,
        engine=MockEngine(seed=11),
    )
    try:
        yield table
    finally:
        table.close()


def test_parse_move_takes_san_uci_and_annotation_variants(play: ModuleType) -> None:
    board = chess.Board()
    assert play.parse_move(board, "Nf3") == chess.Move.from_uci("g1f3")
    assert play.parse_move(board, "g1f3") == chess.Move.from_uci("g1f3")
    assert play.parse_move(board, "Nf3!?") == chess.Move.from_uci("g1f3")
    assert play.parse_move(board, "e2e5") is None
    assert play.parse_move(board, "Qd8") is None
    assert play.parse_move(board, "  ") is None

    castle = chess.Board("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1")
    assert play.parse_move(castle, "0-0") == chess.Move.from_uci("e1g1")
    assert play.parse_move(castle, "0-0-0") == chess.Move.from_uci("e1c1")
    assert play.parse_move(castle, "O-O") == chess.Move.from_uci("e1g1")


def test_clocks_and_move_rows_are_rendered_for_a_player(play: ModuleType) -> None:
    assert play.format_clock(FIXED_TIME_CONTROL.initial_ms) == "30:00.0"
    assert play.format_clock(1_850) == "00:01.8"
    assert play.format_clock(-5) == "00:00.0"
    assert play.numbered_moves(["e4", "e5", "Nf3"]) == ["  1. e4 e5", "  2. Nf3"]


def test_moves_are_labelled_with_their_move_number(play: ModuleType) -> None:
    """``3.`` for white, ``3...`` for black: the ply is what makes a move readable."""
    assert play.move_label(0) == "1."
    assert play.move_label(1) == "1..."
    assert play.move_label(4) == "3."
    assert play.move_label(5) == "3..."
    assert play.move_text(["c4", "e5", "Nc3", "Nf6"]) == "1. c4 e5 2. Nc3 Nf6"
    assert play.move_text([]) == ""


def test_parse_args_validates_the_seed_and_attached_mode(play: ModuleType) -> None:
    options = play._parse_args([])
    assert options.relay is None
    assert options.color == "white"
    assert options.engine == "mock"
    assert options.seed == play.HUMAN_SEED
    assert options.pgn_out is None
    assert play._parse_args(["--pgn-out", "game.pgn"]).pgn_out == Path("game.pgn")
    with pytest.raises(SystemExit):
        play._parse_args(["--seed", "not-hex"])
    with pytest.raises(SystemExit):
        play._parse_args(["--relay", "http://127.0.0.1:8646"])
    with pytest.raises(SystemExit):
        play._parse_args(["--poll-seconds", "0"])


def test_the_solo_table_seats_a_human_and_lets_the_miner_join(play: ModuleType) -> None:
    with _solo(play, clock_ms=lambda: 5_000_000) as table:
        seat = table.seat
        view = seat.view()
        assert view.status == "pending"
        assert seat.address in (view.matchup.white, view.matchup.black)
        assert view.certificate.engine_white == play.HUMAN_ENGINE or (
            view.certificate.engine_black == play.HUMAN_ENGINE
        )
        assert table.relay.witness is not None

        joined = seat.join(view)
        assert seat.color in joined.certificate.start_signatures
        table.step()
        active = seat.view()
        assert active.status == "active"
        assert active.time_control == FIXED_TIME_CONTROL
        assert active.white_ms <= FIXED_TIME_CONTROL.initial_ms
        # The engine joins and, from whichever side it plays, acts on its own.
        assert table.miner.matches[seat.match_id].seat != seat.color
        assert table.errors == []


def test_choose_session_picks_our_seat_and_nothing_else(play: ModuleType) -> None:
    with _solo(play, clock_ms=lambda: 5_000_000) as table:
        views = table.relay.matches()
        match_id = table.seat.match_id
        assert play.choose_session(views, table.seat.address) is not None
        assert play.choose_session(views, table.seat.address, match_id) is not None
        assert play.choose_session(views, table.seat.address, "0" * 64) is None
        assert play.choose_session(views, "sccpt1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq") is None


def test_the_certified_opening_is_announced_before_the_first_prompt(
    play: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The book plies are not the player's and carry no receipts, so they are shown.

    This is the bug a player hits otherwise: the first prompt is move 3 of a game
    they have never seen, so the opponent's next knight move reads as illegal.
    """
    lines: list[str] = []
    with _solo(play, clock_ms=lambda: 5_000_000, ascii=True) as table:
        table.start()
        monkeypatch.setattr("builtins.input", lambda prompt="": "quit")
        game = play.Game(
            table.seat, stack=table, options=play.Options(ascii=True), out=lines.append
        )
        with pytest.raises(play.Quit):
            game.run()

        view = table.seat.view()
        assert view.status == "active"
        opening = play.move_text(view.san_moves)
        assert opening
        assert f"opening  {opening}" in lines
        assert any(line.startswith("you play move ") for line in lines)
        # The board is drawn, so the position the book left is on screen too.
        assert any("a b c d e f g h" in line for line in lines)


def test_a_seed_picks_its_own_opening(play: ModuleType) -> None:
    """``--seed`` derives the beacon, so it also redraws the certified opening."""

    def opening(seed: str) -> tuple[str, ...]:
        table = play._solo_table(
            DEVNET,
            keypair_from_seed(HUMAN_SEED),
            play.Options(seed=seed),
            clock_ms=lambda: 5_000_000,
            engine=MockEngine(seed=11),
        )
        try:
            table.seat.join()
            for _ in range(3):
                table.step()
            return tuple(table.seat.view().san_moves)
        finally:
            table.close()

    seeds = (play.HUMAN_SEED, "aa" * 32, "5a" * 32)
    openings = [opening(seed) for seed in seeds]
    assert all(openings)
    assert len(set(openings)) == len(openings)


def test_the_game_loop_plays_moves_and_reports(
    play: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A whole game, headless: the loop joins, moves, waits, then reports."""
    lines: list[str] = []
    pgn_file = tmp_path / "game.pgn"
    with _solo(play, clock_ms=lambda: 5_000_000, ascii=True) as table:
        table.start()
        ours: list[str] = []

        def answer(prompt: str = "") -> str:
            if len(ours) >= 3:
                return "resign"
            move = next(iter(play._board_from(table.seat.view()).legal_moves)).uci()
            ours.append(move)
            return move

        monkeypatch.setattr("builtins.input", answer)
        game = play.Game(
            table.seat,
            stack=table,
            options=play.Options(ascii=True, pgn_out=pgn_file),
            out=lines.append,
        )
        assert game.run() == 0

        view = table.seat.view()
        assert view.status == "finished"
        assert view.result is not None
        assert view.pgn is not None
        assert pgn_file.read_text(encoding="utf-8") == view.pgn
        bundle = table.seat.receipts()
        assert bundle is not None
        assert view.receipts == len(bundle.receipts)
        assert all(receipt.witnesses for receipt in bundle.receipts)
        assert any("joined as" in line for line in lines)
        assert any(line.startswith("chain    off-chain demo") for line in lines)
        assert any(line.startswith("you played") for line in lines)
        assert any(line.startswith("opponent played") for line in lines)
        assert any(line.startswith("result ") for line in lines)
        assert any(line == f"pgn file {pgn_file}" for line in lines)
        assert any(line == "=== finished ===" for line in lines)


def test_a_flagged_human_is_settled_by_the_engine(play: ModuleType) -> None:
    """Run the clock out: the relay flags, and the miner proves it from receipts."""
    now = [5_000_000]
    with _solo(play, clock_ms=lambda: now[0]) as table:
        seat = table.seat
        seat.join()
        table.step()
        assert seat.view().status == "active"

        # A timeout claim attaches the claimant's last signed move, and the
        # chain refuses a claim from a seat that never moved, so the engine
        # has to answer our move before our clock can run out. Whichever seat
        # the beacon deals us, the board says whether it is our turn.
        view = seat.view()
        board = play._board_from(view)
        if (board.turn == chess.WHITE) == (seat.color == "white"):
            seat.play(board, next(iter(board.legal_moves)), clock_ms=FIXED_TIME_CONTROL.initial_ms)
            table.step()
        assert table.miner.matches[seat.match_id].signed_moves

        now[0] += FIXED_TIME_CONTROL.initial_ms + 5_000
        for _ in range(3):
            table.step()

        finished = seat.view()
        assert finished.status == "finished"
        assert finished.flagged == seat.color
        assert finished.result is not None
        assert finished.result.termination == "time_forfeit"
        assert seat.match_id in table.miner.claims
        assert table.miner.refusals == []
        bundle = seat.receipts()
        assert bundle is not None
        assert bundle.flag is not None
        assert bundle.flag.seat == seat.address


def test_parse_args_validates_on_chain_mode(play: ModuleType) -> None:
    """``--on-chain`` owns its relay and chain, so it rejects attached-mode flags."""
    options = play._parse_args(["--on-chain"])
    assert options.on_chain is True
    assert options.chain_dir is None

    chain_dir = Path("other-chain")
    assert play._parse_args(["--on-chain", "--chain-dir", str(chain_dir)]).chain_dir == chain_dir
    assert play._parse_args(["--on-chain", "--color", "auto"]).color == "auto"

    for argv in (
        ["--on-chain", "--relay", "http://127.0.0.1:8646"],
        ["--on-chain", "--key", "seat.key"],
        ["--on-chain", "--match", "0" * 64],
        ["--chain-dir", str(chain_dir)],
        ["--on-chain", "--network", "testnet"],
    ):
        with pytest.raises(SystemExit):
            play._parse_args(argv)


def _chain_table(play: ModuleType, chain_dir: Path) -> Any:
    """A fresh on-chain solo table with a fixed clock and deterministic engine."""
    return play._chain_table(
        DEVNET,
        keypair_from_seed(HUMAN_SEED),
        play.Options(on_chain=True, chain_dir=chain_dir, color="auto", ascii=True),
        clock_ms=lambda: 5_000_000,
        engine=MockEngine(seed=11),
    )


def _play_to_minimum(play: ModuleType, table: Any) -> Any:
    """Play deterministic legal moves until devnet's proof minimum, then resign."""
    view = table.seat.join()
    for _ in range(400):
        table.step()
        view = table.seat.view()
        if view.status == "finished":
            return view
        if view.status != "active" or view.turn != table.seat.color:
            continue
        if view.last_ply >= DEVNET.min_plies:
            return table.seat.resign()
        board = play._board_from(view)
        view = table.seat.play(
            board,
            next(iter(board.legal_moves)),
            clock_ms=FIXED_TIME_CONTROL.initial_ms,
        )
    raise AssertionError("the game never reached devnet's proof minimum")


def test_the_on_chain_table_bootstraps_a_devnet_chain(play: ModuleType, tmp_path: Path) -> None:
    """The chain path funds both seats, opens epoch 3 and lets the miner join."""
    table = _chain_table(play, tmp_path / "chain")
    try:
        assert table.chain is not None
        assert table.chain_dir == tmp_path / "chain"
        assert (tmp_path / "chain" / play.CHAIN_DB_NAME).exists()
        assert table.chain.height >= play.GENESIS_EPOCH
        assert table.seat.join().status == "pending"
        for _ in range(50):
            table.step()
            if table.seat.view().status == "active":
                break
        assert table.seat.view().status == "active"
        assert table.errors == []
    finally:
        table.close()


def test_an_on_chain_game_mines_its_win_proof(play: ModuleType, tmp_path: Path) -> None:
    """A finished, co-signed game is proved and the proof lands in a new block."""
    table = _chain_table(play, tmp_path / "chain")
    try:
        view = _play_to_minimum(play, table)
        assert view.status == "finished"
        assert view.result is not None

        settlement = table.settle(view)

        assert settlement.mined, settlement.summary
        assert settlement.proof_id is not None
        assert settlement.height == table.chain.height
        stored = table.chain.chain.block_at_height(table.chain.height)
        assert stored is not None
        assert settlement.block_hash == stored.block_hash
        assert any(proof.proof_id == settlement.proof_id for proof in stored.block.proofs)
        # Settling twice reports the same block rather than mining another one.
        height = table.chain.height
        assert table.settle(view) == settlement
        assert table.chain.height == height
        assert table.miner.refusals == []
    finally:
        table.close()


def test_chain_settlement_explains_why_it_did_not_mine(play: ModuleType, tmp_path: Path) -> None:
    """No result, a draw, a flag and a short game each report their own reason."""
    table = _chain_table(play, tmp_path / "chain")
    try:
        view = table.seat.join()
        for _ in range(50):
            table.step()
            view = table.seat.view()
            if view.status == "active" and view.turn == table.seat.color:
                view = table.seat.resign()
                break
        assert view.status == "finished"
        assert view.result is not None

        short = table.settle(view)
        assert not short.mined
        assert "minimum" in short.summary

        table.settlement = None
        draw = view.model_copy(
            update={
                "result": view.result.model_copy(
                    update={"result": "1/2-1/2", "termination": "draw"}
                )
            }
        )
        assert table.settle(draw).summary == "a draw has no winner to prove"

        table.settlement = None
        flagged = view.model_copy(
            update={"result": view.result.model_copy(update={"termination": "time_forfeit"})}
        )
        flagged_settlement = table.settle(flagged)
        assert not flagged_settlement.mined
        assert "time forfeits" in flagged_settlement.summary

        table.settlement = None
        no_result = view.model_copy(update={"result": None})
        assert table.settle(no_result).summary == "the relay has not finished the game"
    finally:
        table.close()


def test_main_reports_a_missing_keyfile_rather_than_crashing(
    play: ModuleType, capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: Any
) -> None:
    """``--relay`` with no usable keyfile exits cleanly with a message."""
    monkeypatch.setenv(play.PASSPHRASE_ENV, "test passphrase")
    assert play.main(["--relay", "http://127.0.0.1:9", "--key", str(tmp_path / "nope.key")]) == 1
    assert "error:" in capsys.readouterr().err
