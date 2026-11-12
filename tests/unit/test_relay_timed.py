"""Timed relay sessions (plan section 5.7.1): clocks, receipts and flags.

These tests drive the relay over its HTTP surface with an injected millisecond
clock, so every second of a game is under the test's control. They check the
three things the M2 relay could not do: a move that the seat signed, a clock
that actually runs, and a flag that ends the game without the losing seat's
cooperation — plus the receipt bundle a timeout claim needs.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from sccp.chess.engines import ScriptedEngine
from sccp.config import DEVNET
from sccp.crypto.address import encode_address
from sccp.crypto.keys import generate_keypair, sign_domain
from sccp.miner.engine_miner import EngineMiner, MinerConfig, RelayClient
from sccp.miner.matchmaker import Matchmaker, SessionStatus, create_app
from sccp.protocol.clocks import (
    FIXED_TIME_CONTROL,
    sign_move,
    verify_timeout_claim,
)
from sccp.protocol.models import (
    MoveMessage,
    SignedMove,
    TimeoutClaim,
)
from sccp.protocol.transcript import certificate_payload
from sccp.serialization import Domain

RELAY_SECONDS = 1_700_000_000
CONTINUATION = (("white", "Qh5"), ("black", "Nf6"), ("white", "Qxf7#"))


@dataclass
class FakeTime:
    """One adjustable clock driving both the relay's clocks and its timestamps."""

    seconds: int = RELAY_SECONDS
    ms: int = 5_000_000

    def now_seconds(self) -> int:
        return self.seconds

    def now_ms(self) -> int:
        return self.ms

    def advance(self, ms: int) -> None:
        self.ms += ms

    def advance_seconds(self, seconds: int) -> None:
        self.seconds += seconds


def _engines(signed_match: Any) -> dict[str, str]:
    return {
        account.address: f"mock-v1:{index:016d}"
        for index, account in enumerate(signed_match.accounts.values(), start=1)
    }


def _relay(
    signed_match: Any,
    *,
    time: FakeTime | None = None,
    witness: Any = None,
    store: Path | None = None,
    timeout: int = 300,
) -> Matchmaker:
    clock = time or FakeTime()
    return Matchmaker(
        signed_match.params,
        beacon=signed_match.beacon,
        roster=signed_match.roster,
        public_keys=signed_match.public_keys,
        engines=_engines(signed_match),
        book=signed_match.book,
        store=store,
        clock=clock.now_seconds,
        clock_ms=clock.now_ms,
        timeout_seconds=timeout,
        witness_keypair=witness,
    )


def _account(signed_match: Any, address: str) -> Any:
    return next(
        account for account in signed_match.accounts.values() if account.address == address
    )


def _join(client: TestClient, signed_match: Any, view: dict[str, Any], seat: str) -> dict[str, Any]:
    address = view["certificate"][seat]
    account = _account(signed_match, address)
    certificate = signed_match.certificate.__class__.model_validate(view["certificate"])
    signature = sign_domain(
        account.keypair, Domain.CERTIFICATE, certificate_payload(certificate)
    ).hex()
    response = client.post(
        f"/v1/matches/{view['matchup']['match_id']}/join",
        json={
            "seat": address,
            "public_key": account.keypair.public_key_hex,
            "signature": signature,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _signed(signed_match: Any, view: dict[str, Any], seat: str, ply: int, san: str) -> Any:
    address = view["certificate"][seat]
    account = _account(signed_match, address)
    message = MoveMessage(
        network=view["certificate"]["network"],
        match_id=view["matchup"]["match_id"],
        seat=address,
        ply=ply,
        san=san,
        clock_ms=0,
        signature="0" * 128,
    )
    return sign_move(message, account.keypair)


def _move_body(
    signed_match: Any, view: dict[str, Any], seat: str, ply: int, san: str
) -> dict[str, Any]:
    signed = _signed(signed_match, view, seat, ply, san)
    return {
        "seat": signed.seat,
        "san": signed.san,
        "network": signed.network,
        "ply": signed.ply,
        "clock_ms": 1_000,
        "signature": signed.signature,
    }


def _start(signed_match: Any, **kwargs: Any) -> tuple[Matchmaker, TestClient, dict[str, Any]]:
    """Open round 0, join both seats and return the active session."""
    state = _relay(signed_match, **kwargs)
    client = TestClient(create_app(state))
    created = client.get("/v1/matches").json()
    assert created == []
    client.post("/v1/rounds", json={"round": 0})
    view = client.get("/v1/matches").json()[0]
    for seat in ("white", "black"):
        view = _join(client, signed_match, view, seat)
    assert view["status"] == "active"
    return state, client, view


def test_activation_starts_both_clocks_at_the_fixed_control(signed_match: Any) -> None:
    state, _client, view = _start(signed_match)
    opening_plies = len(view["certificate"]["opening"]["moves"])
    assert view["time_control"] == {
        "initial_ms": FIXED_TIME_CONTROL.initial_ms,
        "increment_ms": FIXED_TIME_CONTROL.increment_ms,
    }
    assert view["white_ms"] == FIXED_TIME_CONTROL.initial_ms
    assert view["black_ms"] == FIXED_TIME_CONTROL.initial_ms
    assert view["turn"] == "white"
    assert view["last_ply"] == opening_plies
    assert view["receipts"] == 0
    assert view["flagged"] is None
    session = state._sessions[view["matchup"]["match_id"]]
    assert session.status is SessionStatus.ACTIVE
    assert session.clock is not None and session.clock.started


def test_a_signed_move_is_charged_and_receipted(signed_match: Any) -> None:
    time = FakeTime()
    state, client, view = _start(signed_match, time=time, witness=generate_keypair())
    witness = state.witness
    assert witness is not None
    match_id = view["matchup"]["match_id"]
    ply = len(view["san_moves"])

    time.advance(5_000)
    response = client.post(
        f"/v1/matches/{match_id}/move",
        json=_move_body(signed_match, view, "white", ply, CONTINUATION[0][1]),
    )
    assert response.status_code == 200, response.text
    moved = response.json()

    deduction = 5_000 - DEVNET.lag_grace_ms
    assert moved["white_ms"] == FIXED_TIME_CONTROL.initial_ms - deduction
    assert moved["black_ms"] == FIXED_TIME_CONTROL.initial_ms
    assert moved["receipts"] == 1
    assert moved["turn"] == "black"

    bundle = client.get(f"/v1/matches/{match_id}/receipts").json()
    assert bundle["started"] is True
    assert bundle["last_ply"] == ply + 1
    receipt = bundle["receipts"][0]
    assert receipt["ply"] == ply
    assert receipt["san"] == CONTINUATION[0][1]
    assert receipt["seat"] == view["certificate"]["white"]
    assert receipt["white_ms"] == moved["white_ms"]
    assert [item["witness"] for item in receipt["witnesses"]] == [witness]
    assert bundle["flag"] is None


def test_the_clock_only_runs_for_the_side_to_move(signed_match: Any) -> None:
    time = FakeTime()
    _state, client, view = _start(signed_match, time=time, witness=generate_keypair())
    match_id = view["matchup"]["match_id"]
    ply = len(view["san_moves"])

    # White thinks for four seconds, black for one: only the mover is charged.
    time.advance(4_000)
    client.post(
        f"/v1/matches/{match_id}/move",
        json=_move_body(signed_match, view, "white", ply, CONTINUATION[0][1]),
    )
    time.advance(1_000)
    response = client.post(
        f"/v1/matches/{match_id}/move",
        json=_move_body(signed_match, view, "black", ply + 1, CONTINUATION[1][1]),
    )
    assert response.status_code == 200, response.text
    moved = response.json()
    grace = DEVNET.lag_grace_ms
    assert moved["white_ms"] == FIXED_TIME_CONTROL.initial_ms - (4_000 - grace)
    assert moved["black_ms"] == FIXED_TIME_CONTROL.initial_ms - (1_000 - grace)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"ply": 0}, "next move is ply"),
        ({"network": "mainnet"}, "this relay is on"),
        ({"signature": "0" * 128}, "invalid"),
        ({"san": "Qh5+"}, "canonical SAN"),
    ],
)
def test_a_forged_or_inconsistent_move_body_is_refused(
    signed_match: Any, overrides: dict[str, Any], message: str
) -> None:
    time = FakeTime()
    _state, client, view = _start(signed_match, time=time, witness=generate_keypair())
    match_id = view["matchup"]["match_id"]
    ply = len(view["san_moves"])
    body = _move_body(signed_match, view, "white", ply, CONTINUATION[0][1])
    body.update(overrides)
    time.advance(1_000)
    response = client.post(f"/v1/matches/{match_id}/move", json=body)
    assert response.status_code == 400, response.text
    assert message in response.json()["detail"]
    # Nothing was applied: no move, no receipt, no charge.
    current = client.get(f"/v1/matches/{match_id}").json()
    assert current["receipts"] == 0
    assert current["last_ply"] == ply

    assert session.clock is not None and session.clock.started


def test_a_clock_that_reaches_zero_flags_without_the_loser(signed_match: Any) -> None:
    time = FakeTime()
    state, client, view = _start(signed_match, time=time, witness=generate_keypair())
    match_id = view["matchup"]["match_id"]
    ply = len(view["san_moves"])
    time.advance(2_000)
    client.post(
        f"/v1/matches/{match_id}/move",
        json=_move_body(signed_match, view, "white", ply, CONTINUATION[0][1]),
    )

    # Black never answers: the relay's own sweep ends the game.
    time.advance(FIXED_TIME_CONTROL.initial_ms)
    assert state.sweep() == [match_id]

    finished = client.get(f"/v1/matches/{match_id}").json()
    assert finished["status"] == "finished"
    assert finished["flagged"] == "black"
    assert finished["result"]["termination"] == "time_forfeit"
    assert finished["result"]["result"] == "1-0"
    assert finished["white_ms"] > 0
    assert finished["black_ms"] == 0
    assert "TimeControl" in finished["pgn"]

    bundle = client.get(f"/v1/matches/{match_id}/receipts").json()
    assert bundle["flag"]["seat"] == view["certificate"]["black"]
    assert bundle["flag"]["ply"] == len(finished["san_moves"])
    assert bundle["flag"]["black_ms"] == 0
    # The winner never has to trust the relay's word: the flag is signed.
    assert bundle["flag"]["witnesses"][0]["witness"] == state.witness


def test_a_late_move_is_refused_and_the_session_flags_the_mover(signed_match: Any) -> None:
    time = FakeTime()
    _state, client, view = _start(signed_match, time=time, witness=generate_keypair())
    match_id = view["matchup"]["match_id"]
    ply = len(view["san_moves"])
    time.advance(FIXED_TIME_CONTROL.initial_ms + 1_000)
    response = client.post(
        f"/v1/matches/{match_id}/move",
        json=_move_body(signed_match, view, "white", ply, CONTINUATION[0][1]),
    )
    assert response.status_code == 400
    assert "no time left" in response.json()["detail"]
    finished = client.get(f"/v1/matches/{match_id}").json()
    assert finished["status"] == "finished"
    assert finished["flagged"] == "white"
    assert finished["result"]["result"] == "0-1"
    assert finished["san_moves"] == view["san_moves"]




def test_the_relay_flag_settles_as_a_verifiable_timeout_claim(signed_match: Any) -> None:
    time = FakeTime()
    state, client, view = _start(signed_match, time=time, witness=generate_keypair())
    witness = state.witness
    assert witness is not None
    match_id = view["matchup"]["match_id"]
    ply = len(view["san_moves"])
    time.advance(2_000)
    last = _signed(signed_match, view, "white", ply, CONTINUATION[0][1])
    played = client.post(
        f"/v1/matches/{match_id}/move",
        json=_move_body(signed_match, view, "white", ply, CONTINUATION[0][1]),
    )
    assert played.status_code == 200, played.text

    time.advance(FIXED_TIME_CONTROL.initial_ms)
    state.sweep()
    finished = client.get(f"/v1/matches/{match_id}").json()
    bundle = client.get(f"/v1/matches/{match_id}/receipts").json()
    claim = TimeoutClaim.model_validate(
        {
            "network": finished["certificate"]["network"],
            "match_id": match_id,
            "certificate": finished["certificate"],
            "last_move": last.model_dump(mode="json"),
            "clock_evidence": {
                "time_control": bundle["time_control"],
                "final": bundle["flag"],
                "receipts": bundle["receipts"],
            },
            "bond": DEVNET.min_timeout_bond,
        }
    )
    witness_key = _account(signed_match, witness).keypair.public_key_hex
    winner = verify_timeout_claim(
        claim,
        certificate=claim.certificate,
        san_moves=finished["san_moves"],
        public_keys={witness: witness_key},
        quorum=DEVNET.clock_witness_quorum,
        max_plies=DEVNET.max_plies,
        lag_grace_ms=DEVNET.lag_grace_ms,
    )
    assert winner == "white"


def test_a_relay_without_a_witness_key_still_runs_the_clock(signed_match: Any) -> None:
    time = FakeTime()
    state, client, view = _start(signed_match, time=time)
    assert state.witness is None
    match_id = view["matchup"]["match_id"]
    ply = len(view["san_moves"])
    time.advance(3_000)


def test_a_live_clocked_session_is_never_reaped_for_inactivity(signed_match: Any) -> None:
    time = FakeTime()
    state, _client, view = _start(signed_match, time=time, timeout=1)
    match_id = view["matchup"]["match_id"]
    time.advance_seconds(10_000)
    assert state.expire() == []
    assert state.get(match_id).status == "active"
    assert state.reputation() == {}


def test_a_silent_join_is_still_reaped(signed_match: Any) -> None:
    time = FakeTime()
    state = _relay(signed_match, time=time, timeout=1)
    client = TestClient(create_app(state))
    client.post("/v1/rounds", json={"round": 0})
    view = client.get("/v1/matches").json()[0]
    _join(client, signed_match, view, "white")
    time.advance_seconds(10_000)
    assert state.expire() == [view["matchup"]["match_id"]]
    assert state.reputation() == {view["certificate"]["black"]: 1}

    moved = client.post(
        f"/v1/matches/{match_id}/move",
        json=_move_body(signed_match, view, "white", ply, CONTINUATION[0][1]),
    ).json()
    assert moved["white_ms"] == FIXED_TIME_CONTROL.initial_ms - (3_000 - DEVNET.lag_grace_ms)
    bundle = client.get(f"/v1/matches/{match_id}/receipts").json()
    assert bundle["receipts"][0]["witnesses"] == []
