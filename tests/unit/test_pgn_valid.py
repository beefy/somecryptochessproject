"""Valid PGNs and every accepted termination (PROTOCOL.md section 3)."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path

import pytest

from sccp.chess.pgn import (
    GameExpectations,
    GameResult,
    Termination,
    canonical_game_hash,
    validate_pgn,
    validate_pgn_file,
)
from sccp.config import TESTNET, NetworkParams
from sccp.errors import PgnValidationError

FOOLS_MATE = "1. f3 e5 2. g4 Qh4# 0-1"
STALEMATE = (
    "1. e3 a5 2. Qh5 Ra6 3. Qxa5 h5 4. Qxc7 Rah6 5. h4 f6 6. Qxd7+ Kf7 "
    "7. Qxb7 Qd3 8. Qxb8 Qh7 9. Qxc8 Kg6 10. Qe6 1/2-1/2"
)


def _params(**overrides: int) -> NetworkParams:
    return NetworkParams.model_validate({**TESTNET.model_dump(), **overrides})


def test_checkmate_pgn_validates(
    make_pgn: Callable[[str, Mapping[str, str]], str],
    match_tags: dict[str, str],
    expectations: GameExpectations,
) -> None:
    parsed = validate_pgn(make_pgn(FOOLS_MATE, match_tags), params=TESTNET, expected=expectations)

    assert parsed.result is GameResult.BLACK
    assert parsed.termination is Termination.CHECKMATE
    assert parsed.plies == 4
    assert parsed.san_moves == ("f3", "e5", "g4", "Qh4#")
    assert parsed.winner is GameResult.BLACK
    assert parsed.tags["MatchID"] == "m-1"
    assert parsed.final_board.is_checkmate()
    assert parsed.game_hash == canonical_game_hash("testnet", parsed.san_moves, "0-1")
    assert len(parsed.game_hash) == 64
    assert parsed.game_hash == parsed.game_hash.lower()


def test_draw_by_stalemate_validates(
    make_pgn: Callable[[str, Mapping[str, str]], str],
    match_tags: dict[str, str],
    expectations: GameExpectations,
) -> None:
    tags = {**match_tags, "Result": "1/2-1/2", "Termination": "draw"}
    parsed = validate_pgn(make_pgn(STALEMATE, tags), params=TESTNET, expected=expectations)

    assert parsed.result is GameResult.DRAW
    assert parsed.termination is Termination.DRAW
    assert parsed.winner is None
    assert parsed.final_board.is_stalemate()


def test_resignation_is_accepted_without_a_board_outcome(
    make_pgn: Callable[[str, Mapping[str, str]], str],
    match_tags: dict[str, str],
    expectations: GameExpectations,
) -> None:
    tags = {**match_tags, "Result": "0-1", "Termination": "resignation"}
    parsed = validate_pgn(make_pgn("1. e4 e5 0-1", tags), params=TESTNET, expected=expectations)

    assert parsed.result is GameResult.BLACK
    assert parsed.termination is Termination.RESIGNATION
    assert parsed.final_board.outcome(claim_draw=False) is None


def test_resignation_quality_hook_can_reject(
    make_pgn: Callable[[str, Mapping[str, str]], str],
    match_tags: dict[str, str],
) -> None:
    tags = {**match_tags, "Result": "0-1", "Termination": "resignation"}
    pgn = make_pgn("1. e4 e5 0-1", tags)

    with pytest.raises(PgnValidationError, match="quality policy"):
        validate_pgn(pgn, params=TESTNET, resignation_check=lambda board: False)

    parsed = validate_pgn(pgn, params=TESTNET, resignation_check=lambda board: True)
    assert parsed.termination is Termination.RESIGNATION


def test_require_win_applies_the_proof_rules(
    make_pgn: Callable[[str, Mapping[str, str]], str],
    match_tags: dict[str, str],
) -> None:
    pgn = make_pgn(FOOLS_MATE, match_tags)
    parsed = validate_pgn(pgn, params=_params(min_plies=1), require_win=True)
    assert parsed.result.is_decisive

    with pytest.raises(PgnValidationError, match="at least"):
        validate_pgn(pgn, params=TESTNET, require_win=True)

    draw_tags = {**match_tags, "Result": "1/2-1/2", "Termination": "draw"}
    with pytest.raises(PgnValidationError, match="needs a win"):
        validate_pgn(make_pgn(STALEMATE, draw_tags), params=_params(min_plies=1), require_win=True)


def test_time_forfeit_is_negotiated_by_clock_evidence(
    make_pgn: Callable[[str, Mapping[str, str]], str],
    match_tags: dict[str, str],
) -> None:
    tags = {**match_tags, "Result": "1-0", "Termination": "time_forfeit"}
    pgn = make_pgn("1. e4 e5 1-0", tags)

    with pytest.raises(PgnValidationError, match="clock_evidence"):
        validate_pgn(pgn, params=_params(min_plies=20), require_win=True)

    parsed = validate_pgn(
        pgn,
        params=_params(min_plies=20),
        require_win=True,
        clock_evidence=object(),
    )
    assert parsed.termination is Termination.TIME_FORFEIT
    assert parsed.result is GameResult.WHITE
    assert parsed.time_control == "1800+0"
    assert parsed.low_ply is True

    untimed = {tag: value for tag, value in tags.items() if tag != "TimeControl"}
    with pytest.raises(PgnValidationError, match="TimeControl"):
        validate_pgn(
            make_pgn("1. e4 e5 1-0", untimed),
            params=_params(min_plies=20),
            require_win=True,
            clock_evidence=object(),
        )

    with pytest.raises(PgnValidationError, match="time_forfeit"):
        validate_pgn(
            make_pgn(FOOLS_MATE, match_tags),
            params=TESTNET,
            clock_evidence=object(),
        )


def test_time_forfeit_rejects_a_flag_at_ply_zero(
    make_pgn: Callable[[str, Mapping[str, str]], str],
    match_tags: dict[str, str],
) -> None:
    tags = {**match_tags, "Result": "1-0", "Termination": "time_forfeit"}
    with pytest.raises(PgnValidationError, match="no-show"):
        validate_pgn(
            make_pgn("1-0", tags),
            params=TESTNET,
            clock_evidence=object(),
        )


def test_time_control_must_be_whole_seconds_and_match_the_certificate(
    make_pgn: Callable[[str, Mapping[str, str]], str],
    match_tags: dict[str, str],
    expectations: GameExpectations,
) -> None:
    pgn = make_pgn(FOOLS_MATE, match_tags)
    assert validate_pgn(pgn, params=TESTNET, expected=expectations).time_control == "1800+0"

    missing = {tag: value for tag, value in match_tags.items() if tag != "TimeControl"}
    with pytest.raises(PgnValidationError, match="TimeControl"):
        validate_pgn(make_pgn(FOOLS_MATE, missing), params=TESTNET, expected=expectations)
    with pytest.raises(PgnValidationError, match="TimeControl"):
        validate_pgn(make_pgn(FOOLS_MATE, missing), params=_params(min_plies=1))

    for bad in (
        "1800",
        "1800+5",
        "1800.5+0",
        "0+0",
        "86401+0",
        "1800+1000",
        " 1800+0",
        "1800+0 ",
        "1_800+0",
    ):
        tags = {**match_tags, "TimeControl": bad}
        with pytest.raises(PgnValidationError, match="TimeControl"):
            validate_pgn(make_pgn(FOOLS_MATE, tags), params=TESTNET, expected=expectations)


def test_max_plies_is_enforced(
    make_pgn: Callable[[str, Mapping[str, str]], str],
    match_tags: dict[str, str],
) -> None:
    with pytest.raises(PgnValidationError, match="max_plies"):
        validate_pgn(make_pgn(FOOLS_MATE, match_tags), params=_params(min_plies=1, max_plies=2))


def test_opening_prefix_must_match_the_certificate(
    make_pgn: Callable[[str, Mapping[str, str]], str],
    match_tags: dict[str, str],
    expectations: GameExpectations,
) -> None:
    pgn = make_pgn(FOOLS_MATE, match_tags)
    good = replace(expectations, opening_moves=("f3", "e5"))
    assert validate_pgn(pgn, params=TESTNET, expected=good).plies == 4

    bad = replace(expectations, opening_moves=("e4",))
    with pytest.raises(PgnValidationError, match="opening line"):
        validate_pgn(pgn, params=TESTNET, expected=bad)


def test_bytes_input_and_size_cap(
    make_pgn: Callable[[str, Mapping[str, str]], str],
    match_tags: dict[str, str],
) -> None:
    pgn = make_pgn(FOOLS_MATE, match_tags)
    assert validate_pgn(pgn.encode("utf-8"), params=TESTNET).plies == 4

    with pytest.raises(PgnValidationError, match="UTF-8"):
        validate_pgn(b"\xff\xfe not utf-8", params=TESTNET)

    with pytest.raises(PgnValidationError, match="bytes"):
        validate_pgn(pgn, params=_params(max_pgn_bytes=32))


def test_empty_and_non_text_input_are_rejected() -> None:
    with pytest.raises(PgnValidationError, match="empty"):
        validate_pgn("   \n", params=TESTNET)
    with pytest.raises(PgnValidationError, match="str or bytes"):
        validate_pgn(42, params=TESTNET)  # type: ignore[arg-type]


def test_validate_pgn_file(
    tmp_path: Path,
    make_pgn: Callable[[str, Mapping[str, str]], str],
    match_tags: dict[str, str],
) -> None:
    path = tmp_path / "game.pgn"
    path.write_text(make_pgn(FOOLS_MATE, match_tags), encoding="utf-8")
    parsed = validate_pgn_file(path, params=TESTNET)
    assert parsed.result is GameResult.BLACK

    with pytest.raises(PgnValidationError, match="cannot read"):
        validate_pgn_file(tmp_path / "missing.pgn", params=TESTNET)
