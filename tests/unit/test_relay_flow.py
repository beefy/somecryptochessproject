"""The FastAPI relay: a complete two-miner match over ASGI (PROTOCOL.md section 8)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sccp.config import DEVNET
from sccp.crypto.keys import sign_domain
from sccp.errors import RelayError
from sccp.miner.matchmaker import Matchmaker, SessionStatus, create_app
from sccp.node.routes import create_app as create_node_app
from sccp.protocol.models import GameResult, Transcript
from sccp.protocol.transcript import (
    certificate_payload,
    proof_from_transcript,
    result_payload,
)
from sccp.protocol.verification import verify_win_proof
from sccp.serialization import Domain

CONTINUATION = (("white", "Qh5"), ("black", "Nf6"), ("white", "Qxf7#"))


def _engines(signed_match) -> dict[str, str]:
    return {
        account.address: f"mock-v1:{index:016d}"
        for index, account in enumerate(signed_match.accounts.values(), start=1)
    }


def _relay(signed_match, *, store: Path | None = None, clock=None, timeout: int = 300):
    return Matchmaker(
        signed_match.params,
        beacon=signed_match.beacon,
        roster=signed_match.roster,
        public_keys=signed_match.public_keys,
        engines=_engines(signed_match),
        book=signed_match.book,
        store=store,
        clock=clock,
        timeout_seconds=timeout,
    )


def _account_for(signed_match, address: str):
    return next(account for account in signed_match.accounts.values() if account.address == address)


def _join_seat(client: TestClient, signed_match, view: dict, seat: str) -> dict:
    address = view["certificate"][seat]
    account = _account_for(signed_match, address)
    certificate = signed_match.certificate.__class__.model_validate(view["certificate"])
    signature = sign_domain(
        account.keypair,
        Domain.CERTIFICATE,
        certificate_payload(certificate),
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


def _join(client: TestClient, signed_match, view: dict) -> dict:
    for seat in ("white", "black"):
        view = _join_seat(client, signed_match, view, seat)
    return view


def _play(client: TestClient, signed_match, view: dict) -> dict:
    certificate = view["certificate"]
    for seat, san in CONTINUATION:
        response = client.post(
            f"/v1/matches/{view['matchup']['match_id']}/move",
            json={"seat": certificate[seat], "san": san},
        )
        assert response.status_code == 200, response.text
    return client.get(f"/v1/matches/{view['matchup']['match_id']}").json()


def _play(client: TestClient, signed_match, view: dict) -> dict:
    certificate = view["certificate"]
    for seat, san in CONTINUATION:
        response = client.post(
            f"/v1/matches/{view['matchup']['match_id']}/move",
            json={"seat": certificate[seat], "san": san},
        )
        assert response.status_code == 200, response.text
    return client.get(f"/v1/matches/{view['matchup']['match_id']}").json()


def _sign_result(client: TestClient, signed_match, view: dict) -> dict:
    result = GameResult.model_validate(view["result"])
    for seat in ("white", "black"):
        address = view["certificate"][seat]
        account = _account_for(signed_match, address)
        signature = sign_domain(account.keypair, Domain.RESULT, result_payload(result)).hex()
        response = client.post(
            f"/v1/matches/{view['matchup']['match_id']}/result",
            json={"seat": address, "signature": signature},
        )
        assert response.status_code == 200, response.text
    return client.get(f"/v1/matches/{view['matchup']['match_id']}").json()


def test_a_complete_match_produces_a_verifiable_proof(signed_match) -> None:
    state = _relay(signed_match)
    views = state.open_round(0)
    assert len(views) == 1
    match_id = views[0].matchup.match_id
    client = TestClient(create_app(state))

    status = client.get("/v1/status").json()
    assert status["network"] == signed_match.params.network_id
    assert status["matches"] == 1
    assert status["open"] == 1

    view = client.get(f"/v1/matches/{match_id}").json()
    assert view["status"] == SessionStatus.PENDING.value
    view = _join(client, signed_match, view)
    assert view["status"] == SessionStatus.ACTIVE.value
    assert set(view["certificate"]["start_signatures"]) == {"white", "black"}

    view = _play(client, signed_match, view)
    assert view["status"] == SessionStatus.FINISHED.value
    assert view["result"]["result"] == "1-0"
    assert view["result"]["termination"] == "checkmate"
    assert view["result"]["plies"] == len(view["san_moves"])

    view = _sign_result(client, signed_match, view)
    assert set(view["result"]["result_signatures"]) == {"white", "black"}

    transcript = Transcript.model_validate(client.get(f"/v1/matches/{match_id}/transcript").json())
    proof = proof_from_transcript(transcript, params=signed_match.params)
    verified = verify_win_proof(proof, signed_match.context)
    assert verified.parsed.game_hash == transcript.result.game_hash
    assert verified.proof.winner in (
        signed_match.certificate.white,
        signed_match.certificate.black,
    )


def test_relay_rejects_bad_requests(signed_match) -> None:
    state = _relay(signed_match)
    view = state.open_round(0)[0]
    match_id = view.matchup.match_id
    client = TestClient(create_app(state))

    assert client.get("/v1/matches/" + "00" * 32).status_code == 404
    assert client.get(f"/v1/matches/{match_id}/transcript").status_code == 404
    assert (
        client.post(
            "/v1/matches/" + "00" * 32 + "/join",
            json={
                "seat": view.matchup.white,
                "public_key": "00" * 32,
                "signature": "00" * 64,
            },
        ).status_code
        == 400
    )
    assert (
        client.post(
            f"/v1/matches/{match_id}/move",
            json={"seat": view.matchup.white, "san": "e4"},
        ).status_code
        == 400
    )
    with pytest.raises(RelayError):
        state.open_round(0)

    certificate = view.certificate
    joined = _join_seat(client, signed_match, view.model_dump(mode="json"), "white")
    assert joined["status"] == SessionStatus.PENDING.value

    response = client.post(
        f"/v1/matches/{match_id}/move",
        json={"seat": certificate.white, "san": "Qh5"},
    )
    assert response.status_code == 400
    assert "not accepting moves" in response.json()["detail"]

    joined = _join_seat(client, signed_match, joined, "black")
    response = client.post(
        f"/v1/matches/{match_id}/move",
        json={"seat": certificate.black, "san": "e5"},
    )
    assert response.status_code == 400
    assert "turn" in response.json()["detail"]

    response = client.post(
        f"/v1/matches/{match_id}/move",
        json={"seat": certificate.white, "san": "Qh9"},
    )
    assert response.status_code == 400
    assert "legal move" in response.json()["detail"]


def test_relay_restart_preserves_pending_matches_and_transcripts(
    signed_match, tmp_path: Path
) -> None:
    store = tmp_path / "relay.json"
    state = _relay(signed_match, store=store)
    view = state.open_round(0)[0].model_dump(mode="json")
    match_id = view["matchup"]["match_id"]

    first = TestClient(create_app(state))
    view = _join_seat(first, signed_match, view, "white")
    assert view["status"] == SessionStatus.PENDING.value

    restarted = _relay(signed_match, store=store)
    second = TestClient(create_app(restarted))
    pending = second.get(f"/v1/matches/{match_id}").json()
    assert pending["status"] == SessionStatus.PENDING.value
    assert set(pending["certificate"]["start_signatures"]) == {"white"}

    pending = _join_seat(second, signed_match, pending, "black")
    assert pending["status"] == SessionStatus.ACTIVE.value
    pending = _play(second, signed_match, pending)
    pending = _sign_result(second, signed_match, pending)
    transcript = Transcript.model_validate(second.get(f"/v1/matches/{match_id}/transcript").json())
    assert len(transcript.result.result_signatures) == 2

    third = TestClient(create_app(_relay(signed_match, store=store)))
    again = Transcript.model_validate(third.get(f"/v1/matches/{match_id}/transcript").json())
    assert again == transcript
    assert third.get("/v1/status").json()["open"] == 0


def test_relay_expires_silent_matches_and_records_no_shows(signed_match, tmp_path: Path) -> None:
    clock = {"now": 1_000}
    store = tmp_path / "relay.json"
    state = _relay(
        signed_match,
        store=store,
        clock=lambda: clock["now"],
        timeout=10,
    )
    view = state.open_round(0)[0]
    match_id = view.matchup.match_id

    clock["now"] = 1_009
    assert state.expire() == []
    clock["now"] = 1_011
    assert state.expire() == [match_id]
    assert state.get(match_id).status == SessionStatus.ABANDONED.value
    reputation = state.reputation()
    assert reputation == {
        signed_match.certificate.white: 1,
        signed_match.certificate.black: 1,
    }

    restarted = _relay(signed_match, store=store, clock=lambda: clock["now"], timeout=10)
    assert restarted.get(match_id).status == SessionStatus.ABANDONED.value
    assert restarted.reputation() == reputation


def test_relay_config_document_opens_a_round(signed_match, tmp_path: Path) -> None:
    document = {
        "network": signed_match.params.network_id,
        "beacon": signed_match.beacon.model_dump(mode="json"),
        "roster": {
            address: {
                "public_key": signed_match.public_keys[address],
                "engine": f"mock-v1:{index:016d}",
                "max_games": 3,
            }
            for index, address in enumerate(signed_match.roster, start=1)
        },
        "timeout_seconds": 60,
        "open_round": 0,
    }
    state = Matchmaker.from_document(
        document,
        params=signed_match.params,
        store=tmp_path / "relay.json",
    )
    assert len(state.matches()) == 1
    assert state.matches()[0].status == SessionStatus.PENDING.value

    broken = dict(document)
    broken["roster"] = {
        address: {"public_key": signed_match.public_keys[address]}
        for address in signed_match.roster
    }
    with pytest.raises(RelayError, match="engine fingerprint"):
        Matchmaker.from_document(broken, params=signed_match.params)


def test_relay_restarts_over_its_own_store(signed_match, tmp_path: Path) -> None:
    """A relay that restarts must reuse the round it already opened on disk."""
    document = {
        "network": signed_match.params.network_id,
        "beacon": signed_match.beacon.model_dump(mode="json"),
        "roster": {
            address: {
                "public_key": signed_match.public_keys[address],
                "engine": f"mock-v1:{index:016d}",
                "max_games": 3,
            }
            for index, address in enumerate(signed_match.roster, start=1)
        },
        "timeout_seconds": 60,
        "round_seconds": 60,
    }
    store = tmp_path / "relay.json"
    first = Matchmaker.from_document(document, params=signed_match.params, store=store)
    assert len(first.matches()) == 1
    match_id = first.matches()[0].matchup.match_id

    second = Matchmaker.from_document(document, params=signed_match.params, store=store)
    assert [view.matchup.match_id for view in second.matches()] == [match_id]
    assert second.has_round(0)


def test_node_stub_shares_protocol_models(signed_match) -> None:
    app = create_node_app(
        signed_match.params,
        beacons={signed_match.beacon.epoch: signed_match.beacon},
    )
    client = TestClient(app)
    assert client.get("/v1/status").json()["network"] == DEVNET.network_id
    params = client.get("/v1/params").json()
    assert params["network_id"] == DEVNET.network_id
    beacon = client.get(f"/v1/epochs/{signed_match.beacon.epoch}/beacon").json()
    assert beacon["hash"] == signed_match.beacon.hash
    assert client.get("/v1/epochs/999/beacon").status_code == 404
    assert client.get(f"/v1/matches/{signed_match.certificate.match_id}").status_code == 404


def test_rounds_endpoint_and_join_validation(signed_match) -> None:
    state = _relay(signed_match)
    client = TestClient(create_app(state))
    rounds = client.post("/v1/rounds", json={"round": 0})
    assert rounds.status_code == 200, rounds.text
    view = rounds.json()[0]
    match_id = view["matchup"]["match_id"]
    certificate = view["certificate"]
    assert client.post("/v1/rounds", json={"round": 0}).status_code == 400

    carol = signed_match.accounts["carol"]
    response = client.post(
        f"/v1/matches/{match_id}/join",
        json={
            "seat": certificate["white"],
            "public_key": carol.keypair.public_key_hex,
            "signature": "00" * 64,
        },
    )
    assert response.status_code == 400
    assert "registered key" in response.json()["detail"]

    response = client.post(
        f"/v1/matches/{match_id}/join",
        json={
            "seat": carol.address,
            "public_key": carol.keypair.public_key_hex,
            "signature": "00" * 64,
        },
    )
    assert response.status_code == 400
    assert "not a seat" in response.json()["detail"]

    joined = _join_seat(client, signed_match, view, "white")
    response = client.post(
        f"/v1/matches/{match_id}/join",
        json={
            "seat": certificate["white"],
            "public_key": signed_match.public_keys[certificate["white"]],
            "signature": joined["certificate"]["start_signatures"]["white"],
        },
    )
    assert response.status_code == 400
    assert "already joined" in response.json()["detail"]


def test_resignation_over_http_also_verifies(signed_match) -> None:
    state = _relay(signed_match)
    view = state.open_round(0)[0].model_dump(mode="json")
    match_id = view["matchup"]["match_id"]
    client = TestClient(create_app(state))
    view = _join(client, signed_match, view)

    white = view["certificate"]["white"]
    response = client.post(f"/v1/matches/{match_id}/resign", json={"seat": white})
    assert response.status_code == 200, response.text
    view = response.json()
    assert view["result"]["termination"] == "resignation"
    assert view["result"]["result"] == "0-1"

    view = _sign_result(client, signed_match, view)
    transcript = Transcript.model_validate(client.get(f"/v1/matches/{match_id}/transcript").json())
    proof = proof_from_transcript(transcript, params=signed_match.params)
    assert verify_win_proof(proof, signed_match.context).proof.match_id == match_id


def test_corrupt_relay_store_is_rejected(signed_match, tmp_path: Path) -> None:
    malformed = tmp_path / "bad.json"
    malformed.write_text("{not json", encoding="utf-8")
    with pytest.raises(RelayError, match="cannot read relay store"):
        _relay(signed_match, store=malformed)

    wrong_network = tmp_path / "wrong.json"
    wrong_network.write_text(
        '{"format": 1, "network": "testnet", "sessions": [], "no_shows": {}}',
        encoding="utf-8",
    )
    with pytest.raises(RelayError, match="belongs to network"):
        _relay(signed_match, store=wrong_network)

    bad_session = tmp_path / "session.json"
    bad_session.write_text(
        '{"format": 1, "network": "devnet", "sessions": [{}], "no_shows": {}}',
        encoding="utf-8",
    )
    with pytest.raises(RelayError, match="stored match session is invalid"):
        _relay(signed_match, store=bad_session)
