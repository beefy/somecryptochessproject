"""Independent clock witnesses: audit the relay, sign their own evidence.

The relay signs receipts as it accepts moves, but it is one process with one key.
These tests cover the second signer: it polls the relay, verifies what the relay
signed, co-signs the stream with its own key, builds a checkpoint of its own, and
serves a bundle a miner can settle a timeout claim from even when the relay has
no witness key at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi.testclient import TestClient

from sccp.chess.engines import ScriptedEngine
from sccp.config import CLOCK_EVIDENCE_PLIES, DEVNET
from sccp.crypto.address import encode_address
from sccp.crypto.pq import generate_hybrid_keypair, hybrid_sign_domain
from sccp.miner import engine_miner as engine_miner_module
from sccp.miner.engine_miner import EngineMiner, MinerConfig
from sccp.miner.matchmaker import Matchmaker, create_app
from sccp.protocol.clocks import FIXED_TIME_CONTROL, sign_move, verify_clock_evidence
from sccp.protocol.models import ClockEvidence, MoveMessage
from sccp.protocol.transcript import certificate_payload
from sccp.serialization import Domain
from sccp.transport import client_for
from sccp.witness import WitnessService, create_witness_app

RELAY_SECONDS = 1_700_000_000
OPENING_MOVE = "Qh5"


@dataclass
class FakeTime:
    """One adjustable clock driving the relay's clocks and timestamps."""

    seconds: int = RELAY_SECONDS
    ms: int = 5_000_000

    def now_seconds(self) -> int:
        return self.seconds

    def now_ms(self) -> int:
        return self.ms

    def advance(self, ms: int) -> None:
        self.ms += ms


def _engines(signed_match: Any) -> dict[str, str]:
    return {
        account.address: f"mock-v1:{index:016d}"
        for index, account in enumerate(signed_match.accounts.values(), start=1)
    }


def _relay(signed_match: Any, *, time: FakeTime, witness: Any = None) -> Matchmaker:
    return Matchmaker(
        signed_match.params,
        beacon=signed_match.beacon,
        roster=signed_match.roster,
        public_keys=signed_match.public_keys,
        engines=_engines(signed_match),
        book=signed_match.book,
        clock=time.now_seconds,
        clock_ms=time.now_ms,
        timeout_seconds=300,
        witness_keypair=witness,
    )


def _account(signed_match: Any, address: str) -> Any:
    return next(account for account in signed_match.accounts.values() if account.address == address)


def _join(client: TestClient, signed_match: Any, view: dict[str, Any], seat: str) -> dict[str, Any]:
    address = view["certificate"][seat]
    account = _account(signed_match, address)
    certificate = signed_match.certificate.__class__.model_validate(view["certificate"])
    signature = hybrid_sign_domain(
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


def _move_body(signed_match: Any, view: dict[str, Any], seat: str, san: str) -> dict[str, Any]:
    address = view["certificate"][seat]
    account = _account(signed_match, address)
    signed = sign_move(
        MoveMessage(
            network=view["certificate"]["network"],
            match_id=view["matchup"]["match_id"],
            seat=address,
            ply=len(view["san_moves"]),
            san=san,
            clock_ms=0,
            signature="0" * 128,
        ),
        account.keypair,
    )
    return {
        "seat": signed.seat,
        "san": signed.san,
        "network": signed.network,
        "ply": signed.ply,
        "clock_ms": 1_000,
        "signature": signed.signature,
    }


def _active(
    signed_match: Any, time: FakeTime, witness: Any = None
) -> tuple[Any, TestClient, dict[str, Any]]:
    """Open round 0, seat both players and return the relay and its client."""
    state = _relay(signed_match, time=time, witness=witness)
    app = create_app(state)
    client = TestClient(app)
    client.post("/v1/rounds", json={"round": 0})
    view = client.get("/v1/matches").json()[0]
    for seat in ("white", "black"):
        view = _join(client, signed_match, view, seat)
    assert view["status"] == "active"
    return state, client, view


def _flag(
    signed_match: Any, state: Any, client: TestClient, view: dict[str, Any], time: FakeTime
) -> None:
    """Play one move, then run black out of clock."""
    match_id = view["matchup"]["match_id"]
    played = client.post(
        f"/v1/matches/{match_id}/move", json=_move_body(signed_match, view, "white", OPENING_MOVE)
    )
    assert played.status_code == 200, played.text
    time.advance(FIXED_TIME_CONTROL.initial_ms + 5_000)
    state.sweep()
    assert client.get(f"/v1/matches/{match_id}").json()["flagged"] == "black"


def _observed_flag(
    signed_match: Any,
    service: WitnessService,
    state: Any,
    client: TestClient,
    view: dict[str, Any],
    time: FakeTime,
) -> None:
    """Run a match to a flag while the witness polls, as a live one would.

    The witness can only attest to matches it watched: it learns a match while it
    is pending or active, so the game has to happen *around* the polls.
    """
    service.poll()  # the match is open, no receipts yet
    _flag(signed_match, state, client, view, time)
    service.poll()  # one move receipt, the flag, and the checkpoint


def _address(keypair: Any) -> str:
    return encode_address(keypair.public_key_bytes, DEVNET)


def _service(app: Any, keypair: Any, *, relay_keypair: Any = None) -> WitnessService:
    keys = {} if relay_keypair is None else {_address(relay_keypair): relay_keypair.public_key_hex}
    return WitnessService(
        DEVNET,
        keypair,
        relay_url="http://relay",
        relay_keys=keys,
        client=client_for(app, base_url="http://relay"),
    )


def test_a_witness_signs_every_receipt_and_builds_its_own_checkpoint(signed_match: Any) -> None:
    time = FakeTime()
    relay_key = generate_hybrid_keypair()
    state, client, view = _active(signed_match, time, relay_key)
    service = _service(create_app(state), generate_hybrid_keypair(), relay_keypair=relay_key)
    _observed_flag(signed_match, service, state, client, view, time)
    match_id = view["matchup"]["match_id"]

    bundle = service.bundle(match_id)
    assert bundle.flag is not None
    relay_address = _address(relay_key)
    for receipt in (*bundle.receipts, bundle.flag):
        assert {entry.witness for entry in receipt.witnesses} == {relay_address, service.address}

    # Our bundle is a complete quorum-1 attestation on its own: the checkpoint is
    # ours, every receipt carries our signature, and the protocol's verifier agrees.
    assert bundle.checkpoint is not None
    evidence = ClockEvidence(
        time_control=bundle.time_control,
        final=bundle.flag,
        receipts=bundle.receipts[-CLOCK_EVIDENCE_PLIES:],
        checkpoint=bundle.checkpoint,
    )
    verify_clock_evidence(
        evidence,
        certificate=state.get(match_id).certificate,
        san_moves=client.get(f"/v1/matches/{match_id}").json()["san_moves"],
        result="1-0",
        winner_seat="white",
        public_keys={
            service.address: service.public_key,
            relay_address: relay_key.public_key_hex,
        },
        quorum=1,
        max_plies=DEVNET.max_plies,
    )
    assert service.status()["audited"] == 1
    assert service.audit(match_id)["verified"] is True
    assert service.matches()[0]["signed"] == len(bundle.receipts) + 1


def test_the_witness_api_serves_its_bundle_and_audit(signed_match: Any) -> None:
    time = FakeTime()
    relay_key = generate_hybrid_keypair()
    state, client, view = _active(signed_match, time, relay_key)
    service = _service(create_app(state), generate_hybrid_keypair(), relay_keypair=relay_key)
    _observed_flag(signed_match, service, state, client, view, time)
    match_id = view["matchup"]["match_id"]

    published = TestClient(create_witness_app(service))
    status = published.get("/v1/status").json()
    assert status["address"] == service.address
    assert status["quorum"] == DEVNET.clock_witness_quorum
    served = published.get(f"/v1/matches/{match_id}/receipts").json()
    assert served["flag"] is not None
    assert served["checkpoint"]["kind"] == "clock"
    assert published.get(f"/v1/matches/{match_id}/audit").json()["verified"] is True
    assert published.get("/v1/matches/unknown/receipts").status_code == 404


def test_a_witness_reports_a_relay_that_signs_nothing(signed_match: Any) -> None:
    """A relay without ``--witness-key`` is exactly what the stack used to be."""
    time = FakeTime()
    state, client, view = _active(signed_match, time, witness=None)
    service = _service(create_app(state), generate_hybrid_keypair())
    _observed_flag(signed_match, service, state, client, view, time)
    match_id = view["matchup"]["match_id"]

    assert any("unsigned" in reason for reason in service.status()["refusals"])
    bundle = service.bundle(match_id)
    assert bundle.flag is not None
    assert {entry.witness for entry in bundle.flag.witnesses} == {service.address}
    # Our own attestation still verifies: it is the relay that attests to nothing.
    assert service.audit(match_id)["verified"] is True


def test_a_witness_refuses_a_receipt_signed_by_an_untrusted_key(signed_match: Any) -> None:
    """A key the witness was not given is not a key it will sign over."""
    time = FakeTime()
    state, client, view = _active(signed_match, time, generate_hybrid_keypair())
    service = _service(
        create_app(state),
        generate_hybrid_keypair(),
        relay_keypair=generate_hybrid_keypair(),  # a key the relay never had
    )
    _observed_flag(signed_match, service, state, client, view, time)

    assert service.status()["refusals"], "an unverifiable signature must be reported"
    assert service.matches()[0]["signed"] == 0


def test_a_miner_falls_back_to_a_witness_bundle(signed_match: Any, monkeypatch: Any) -> None:
    """The relay is one process; a claim must survive it losing its witness.

    With no relay witness key the relay publishes receipts but no checkpoint, so
    its own bundle cannot settle a timeout. The miner asks the configured
    witnesses instead and takes the first complete, signed bundle.
    """
    time = FakeTime()
    state, client, view = _active(signed_match, time, witness=None)
    service = _service(create_app(state), generate_hybrid_keypair())
    _observed_flag(signed_match, service, state, client, view, time)
    match_id = view["matchup"]["match_id"]
    bundle = service.bundle(match_id)
    assert bundle.checkpoint is not None

    class _WitnessClient:
        """A RelayClient that answers with one witness's bundle."""

        def __init__(self) -> None:
            self.urls: list[str] = []

        def receipts(self, wanted: str) -> Any:
            return bundle if wanted == match_id else None

    stub = _WitnessClient()
    monkeypatch.setattr(engine_miner_module, "RelayClient", lambda url, **_kwargs: stub)
    miner = EngineMiner(
        MinerConfig(
            params=DEVNET,
            keypair=generate_hybrid_keypair(),
            witness_urls=("http://witness-2:8647",),
        ),
        engine=ScriptedEngine([]),
    )

    assert miner._witness_bundle(match_id) is bundle
    assert miner.refusals == []
