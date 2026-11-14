"""The synthetic quality speaker: re-run, disagree, and replay the challenge.

A node only opens a quality challenge when a proof's summary disagrees with its
own re-run (``sccp.node.quality_sampler``), and testnet's ``quality_mode`` is off,
so the path from an accepted proof to a moved bond never runs on a hosted
testnet. ``synthetic_quality_events`` walks that path from outside the node: it
drives the node's own sampler over the proofs in a block and replays
``open_challenge``/``adjudicate_with_panel`` in an in-process mirror, so nothing
it does can invalidate a real proof.

These tests pin the speaker with a uniform engine (agrees), a losing engine
(disagrees), a crashing engine (fails) and a fake node, so no Stockfish process
starts and no socket opens.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence

import chess
import httpx
import pytest
from deploy import synthetic_quality_events as quality

from sccp.chess.quality import QUALITY_V1, QualityProfile
from sccp.config import NetworkParams
from sccp.crypto.address import validate_address
from sccp.errors import QualityError
from sccp.protocol.models import WinProof

NODE = "http://node"

PROFILE = QualityProfile(
    policy=QUALITY_V1.policy,
    engine=QUALITY_V1.engine,
    depth=1,
    max_cpl=50,
    decided_cp=1_000,
    max_blunders_per_side=0,
    resign_cp=300,
    min_plies=1,
)
"""``quality-v1`` at depth one with a hair trigger: one avoidable centipawn fails."""


class _UniformEngine:
    """Every position is equal, so the honest proof a clean miner posts agrees."""

    name = "uniform-analysis"

    @property
    def fingerprint(self) -> str:
        return "uniform-analysis-v1"

    def analyse(self, board, limit, *, multipv=1):
        legal = list(board.legal_moves)
        if not legal:
            return [
                {
                    "score": chess.engine.PovScore(chess.engine.Mate(0), board.turn),
                    "pv": [],
                }
            ]
        return [
            {
                "score": chess.engine.PovScore(chess.engine.Cp(0), board.turn),
                "pv": [legal[0]],
            }
        ]


class _LosingEngine(_UniformEngine):
    """Every move drops nine pawns, so any played move is a blunder."""

    name = "uniform-900"

    @property
    def fingerprint(self) -> str:
        return "uniform-900-v1"

    def analyse(self, board, limit, *, multipv=1):
        legal = list(board.legal_moves)
        if not legal:
            return super().analyse(board, limit, multipv=multipv)
        return [
            {
                "score": chess.engine.PovScore(chess.engine.Cp(900), board.turn),
                "pv": [legal[0]],
            }
        ]


class _BrokenEngine(_UniformEngine):
    """A re-run that cannot finish."""

    name = "broken"

    def __init__(self) -> None:
        self.attempts = 0

    @property
    def fingerprint(self) -> str:
        return "broken"

    def analyse(self, board, limit, *, multipv=1):
        self.attempts += 1
        raise QualityError("engine crashed")


def _block(height: int, *, proofs: Sequence[WinProof], miner: str) -> dict[str, object]:
    """A ``GET /v1/blocks/{height}`` body carrying ``proofs`` in wire order."""
    return {
        "network": "devnet",
        "hash": f"{height:064x}",
        "height": height,
        "block": {
            "header": {"miner": miner},
            "proofs": [proof.model_dump(mode="json") for proof in proofs],
        },
    }


class _Node:
    """The slice of the node's HTTP API a quality audit reads."""

    def __init__(
        self,
        *,
        tip: int = 5,
        blocks: Mapping[int, Mapping[str, object]] | None = None,
        roster: Mapping[str, str] | None = None,
        status_status: int = 200,
        blocks_status: int = 200,
        epoch_status: int = 200,
    ) -> None:
        self.tip = tip
        self.blocks = dict(blocks or {})
        self.roster = {} if roster is None else dict(roster)
        self.status_status = status_status
        self.blocks_status = blocks_status
        self.epoch_status = epoch_status
        self.calls: list[str] = []
        self.client = httpx.Client(transport=httpx.MockTransport(self._handle), base_url=NODE)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(path)
        if path == "/v1/status":
            if self.status_status != 200:
                return httpx.Response(self.status_status, json={"error": "unavailable"})
            return httpx.Response(200, json={"network": "devnet", "height": self.tip})
        if path.startswith("/v1/blocks/"):
            height = int(path.rsplit("/", 1)[-1])
            if self.blocks_status != 200:
                return httpx.Response(self.blocks_status, json={"error": "no such block"})
            body = self.blocks.get(height)
            return httpx.Response(
                200,
                json=dict(body) if body is not None else _block(height, proofs=(), miner="empty"),
            )
        if path.startswith("/v1/epochs/"):
            if self.epoch_status != 200:
                return httpx.Response(self.epoch_status, json={"error": "no such epoch"})
            epoch = int(path.rsplit("/", 1)[-1])
            return httpx.Response(
                200,
                json={
                    "epoch": epoch,
                    "roster": {
                        address: {"public_key": key} for address, key in self.roster.items()
                    },
                    "roster_root": "cd" * 32,
                },
            )
        raise AssertionError(f"unexpected request {request.url}")


@pytest.fixture
def node() -> Iterator[Callable[..., _Node]]:
    """A fake node per build, with every client closed on the way out."""
    nodes: list[_Node] = []

    def build(**kwargs: object) -> _Node:
        fake = _Node(**kwargs)
        nodes.append(fake)
        return fake

    yield build
    for fake in nodes:
        fake.client.close()


def _audit(
    fake: _Node,
    params: NetworkParams,
    *,
    engine: object | None = None,
    **overrides: object,
) -> quality.QualityAudit:
    """An auditor over ``fake`` that re-runs with ``engine`` instead of Stockfish."""
    return quality.QualityAudit(
        client=fake.client,
        params=params,
        profile=PROFILE,
        engine_factory=(lambda: engine) if engine is not None else None,
        **overrides,
    )


def test_the_challenger_and_the_lie_it_tells_are_fixed(
    signed_match, protocol_params: NetworkParams
) -> None:
    address = quality.challenger_address(protocol_params)
    assert address == quality.challenger_address(protocol_params)
    assert validate_address(address, protocol_params)
    assert address != quality.challenger_address(
        protocol_params.model_copy(update={"address_hrp": "sccp"})
    )

    summary = quality.fabricate_summary(PROFILE)
    assert (summary.policy, summary.verdict) == (PROFILE.policy, "pass")
    assert summary.trace_hash == "00" * 32
    assert summary.engine_fingerprint is None

    proof = signed_match.proof
    assert proof.quality_summary is None
    lie = quality.lying_proof(proof, PROFILE)
    assert (lie.proof_id, lie.game_hash, lie.pgn) == (proof.proof_id, proof.game_hash, proof.pgn)
    assert lie.quality_summary == summary


def test_the_block_reader_takes_proofs_in_wire_order(signed_match) -> None:
    proof = signed_match.proof
    second = proof.model_copy(update={"proof_id": "01" * 32})
    view = _block(5, proofs=[second, proof], miner="sccp1blockminer")

    assert quality.block_proofs(view, 5) == (second, proof)
    assert quality.block_miner(view, 5) == "sccp1blockminer"
    assert quality.block_proofs(_block(5, proofs=(), miner="empty"), 5) == ()


@pytest.mark.parametrize(
    "view",
    [{}, {"block": []}, {"block": {"proofs": "nope"}}, {"block": {"proofs": [{"no": "proof"}]}}],
)
def test_the_block_reader_rejects_a_malformed_block(view: Mapping[str, object]) -> None:
    with pytest.raises(quality.QualityEventError):
        quality.block_proofs(view, 5)


def test_the_block_reader_needs_a_sealed_header() -> None:
    with pytest.raises(quality.QualityEventError, match="block 5 has no miner"):
        quality.block_miner({"block": {"proofs": []}}, 5)
    with pytest.raises(quality.QualityEventError, match="block 5 has no miner"):
        quality.block_miner({}, 5)


def test_an_honest_proof_passes_without_a_challenge(
    node, signed_match, protocol_params: NetworkParams, protocol_public_keys: Mapping[str, str]
) -> None:
    miner = sorted(protocol_public_keys)[0]
    proof = signed_match.proof
    fake = node(tip=5, blocks={5: _block(5, proofs=[proof], miner=miner)})

    audit = _audit(fake, protocol_params, engine=_UniformEngine(), inject_every=2)
    report = audit.audit()

    assert report.analysed == 1
    assert (report.challenged, report.rejected, report.upheld) == (0, 0, 0)
    event = report.events[0]
    assert (event.height, event.proof_id, event.miner) == (5, proof.proof_id, miner)
    assert event.origin == "sampled"
    assert event.verdict == "pass"
    assert event.reasons == ()
    assert event.error is None
    assert not event.challenged
    assert event.status == "none"
    line = event.as_line()
    assert "block 5" in line and "(sampled, bucket " in line and line.endswith("verdict pass")
    assert report.as_line().startswith("quality: block 5")


def test_a_lying_proof_is_challenged_and_the_replay_rejects_the_lie(
    node, signed_match, protocol_params: NetworkParams, protocol_public_keys: Mapping[str, str]
) -> None:
    miner = sorted(protocol_public_keys)[0]
    proof = signed_match.proof
    fake = node(
        tip=5,
        blocks={5: _block(5, proofs=[proof], miner=miner)},
        roster=protocol_public_keys,
    )

    audit = _audit(fake, protocol_params, engine=_UniformEngine(), inject_every=1)
    report = audit.audit()

    event = report.events[0]
    assert event.origin == "injected"
    assert event.verdict == "pass"  # the re-run agrees with the verdict it was sold
    assert event.reasons  # ... but not with the trace the summary commits to
    assert event.note == ""
    resolution = event.resolution
    assert resolution is not None
    assert resolution.status == "rejected"
    assert len(resolution.panel) == 1
    assert resolution.bond == max(1, protocol_params.min_bond)
    assert (resolution.returned, resolution.burned) == (0, resolution.bond)
    assert resolution.slashed is False
    assert resolution.survived is True
    assert (report.challenged, report.rejected, report.upheld) == (1, 1, 0)
    assert "challenge rejected" in event.as_line() and "bond burned" in event.as_line()


def test_a_bad_proof_uphelds_the_challenge_and_slashes_the_miner(
    node, signed_match, protocol_params: NetworkParams, protocol_public_keys: Mapping[str, str]
) -> None:
    miner = sorted(protocol_public_keys)[0]
    proof = signed_match.proof
    fake = node(
        tip=5,
        blocks={5: _block(5, proofs=[proof], miner=miner)},
        roster=protocol_public_keys,
    )

    audit = _audit(fake, protocol_params, engine=_LosingEngine(), inject_every=1)
    event = audit.audit().events[0]

    assert event.origin == "injected"
    assert event.verdict == "fail"
    resolution = event.resolution
    assert resolution is not None
    assert resolution.status == "upheld"
    assert resolution.returned == resolution.bond
    assert resolution.burned == 0
    assert resolution.slashed is True
    assert resolution.survived is False
    assert "challenge upheld" in event.as_line() and "bond returned" in event.as_line()


def test_an_engine_failure_is_reported_rather_than_challenged(
    node, signed_match, protocol_params: NetworkParams, protocol_public_keys: Mapping[str, str]
) -> None:
    miner = sorted(protocol_public_keys)[0]
    proof = signed_match.proof
    fake = node(tip=5, blocks={5: _block(5, proofs=[proof], miner=miner)})

    engine = _BrokenEngine()
    audit = _audit(fake, protocol_params, engine=engine, inject_every=1)
    report = audit.audit()

    event = report.events[0]
    assert engine.attempts == 2  # the sampler's own retry budget, not a single try
    assert event.error == "engine crashed"
    assert event.reasons == ("analysis failed after 2 attempts: engine crashed",)
    assert event.verdict is None
    assert not event.challenged
    assert report.challenged == 0
    assert event.as_line().endswith("analysis failed: engine crashed")


def test_the_audit_follows_the_chain_forward(
    node, signed_match, protocol_params: NetworkParams, protocol_public_keys: Mapping[str, str]
) -> None:
    miner = sorted(protocol_public_keys)[0]
    first = signed_match.proof.model_copy(update={"proof_id": "01" * 32})
    second = signed_match.proof.model_copy(update={"proof_id": "02" * 32})
    fake = node(
        tip=6,
        blocks={
            5: _block(5, proofs=[first], miner=miner),
            6: _block(6, proofs=[second], miner=miner),
        },
        roster=protocol_public_keys,
    )
    audit = _audit(fake, protocol_params, engine=_UniformEngine(), max_events=2)

    report = audit.audit()
    assert [event.height for event in report.events] == [5, 6]
    assert [event.origin for event in report.events] == ["sampled", "injected"]
    assert report.analysed == 2

    again = audit.audit()
    assert again.events == ()
    assert again.blocked == "no block after 6"
    assert again.as_line() == "quality: no block after 6"


def test_a_window_without_a_proof_says_so(node, protocol_params: NetworkParams) -> None:
    fake = node(tip=4)
    opened: list[int] = []
    audit = quality.QualityAudit(
        client=fake.client,
        params=protocol_params,
        profile=PROFILE,
        engine_factory=lambda: opened.append(1) or _UniformEngine(),
    )

    report = audit.audit()

    assert report.events == ()
    assert report.blocked == "no new proof between blocks 1 and 4"
    assert report.as_line() == "quality: no new proof between blocks 1 and 4"
    assert opened == []
    assert [call for call in fake.calls if call.startswith("/v1/blocks/")] == [
        f"/v1/blocks/{height}" for height in (1, 2, 3, 4)
    ]


def test_a_network_without_a_challenge_window_skips_the_audit(
    node, protocol_params: NetworkParams
) -> None:
    fake = node(tip=5)
    params = protocol_params.model_copy(update={"challenge_window": 0})

    report = _audit(fake, params, engine=_UniformEngine()).audit()

    assert report.events == ()
    assert report.blocked == "this network disables quality challenges"
    assert fake.calls == []


def test_a_roster_that_cannot_seat_a_panel_is_a_note(
    node, signed_match, protocol_params: NetworkParams
) -> None:
    proof = signed_match.proof
    fake = node(tip=5, blocks={5: _block(5, proofs=[proof], miner="sccp1lonelyminer")})

    audit = _audit(fake, protocol_params, engine=_UniformEngine(), inject_every=1)
    report = audit.audit()

    event = report.events[0]
    assert event.reasons  # the lie was still detected
    assert event.resolution is None
    assert not event.challenged
    assert "no bonded miner" in event.note
    assert event.as_line().endswith(f"; {event.note}")


def test_an_owned_engine_is_opened_lazily_and_closed_once(
    node,
    signed_match,
    protocol_params: NetworkParams,
    protocol_public_keys: Mapping[str, str],
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    miner = sorted(protocol_public_keys)[0]
    proof = signed_match.proof
    fake = node(tip=5, blocks={5: _block(5, proofs=[proof], miner=miner)})
    engine = _UniformEngine()
    opened: list[str] = []
    engine.close = lambda: opened.append("closed")  # type: ignore[method-assign]
    monkeypatch.setattr(
        quality,
        "open_analysis_engine",
        lambda _path, _profile: opened.append("opened") or engine,
    )
    binary = tmp_path / "stockfish"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    audit = quality.QualityAudit(
        client=fake.client,
        params=protocol_params,
        profile=PROFILE,
        engine_path=binary,
    )

    audit.audit()
    assert opened == ["opened"]

    fake.tip = 6
    second = proof.model_copy(update={"proof_id": "03" * 32})
    fake.blocks[6] = _block(6, proofs=[second], miner=miner)
    audit.audit()
    assert opened == ["opened"]  # the engine is opened once and reused

    audit.close()
    audit.close()  # idempotent
    assert opened == ["opened", "closed"]
    with pytest.raises(quality.QualityEventError, match="this auditor is closed"):
        audit.audit()


def test_a_missing_engine_binary_is_one_clear_error(
    node,
    signed_match,
    protocol_params: NetworkParams,
    protocol_public_keys: Mapping[str, str],
    tmp_path,
) -> None:
    miner = sorted(protocol_public_keys)[0]
    proof = signed_match.proof
    fake = node(tip=5, blocks={5: _block(5, proofs=[proof], miner=miner)})
    audit = quality.QualityAudit(
        client=fake.client,
        params=protocol_params,
        profile=PROFILE,
        engine_path=tmp_path / "missing-stockfish",
    )

    with pytest.raises(quality.QualityEventError, match="no engine binary"):
        audit.audit()


def test_a_node_that_cannot_be_read_is_a_cycle_failure(
    node, signed_match, protocol_params: NetworkParams, protocol_public_keys: Mapping[str, str]
) -> None:
    unavailable = node(tip=5, status_status=503)
    with pytest.raises(quality.QualityEventError, match="GET /v1/status failed: 503"):
        _audit(unavailable, protocol_params, engine=_UniformEngine()).audit()

    miner = sorted(protocol_public_keys)[0]
    proof = signed_match.proof
    missing = node(tip=5, blocks={5: _block(5, proofs=[proof], miner=miner)}, blocks_status=404)
    with pytest.raises(quality.QualityEventError, match="GET /v1/blocks/1 failed: 404"):
        _audit(missing, protocol_params, engine=_UniformEngine()).audit()


def test_the_constructor_refuses_unusable_budgets(node, protocol_params: NetworkParams) -> None:
    client = node(tip=0).client
    for overrides, message in (
        ({"max_events": 0}, "max_events"),
        ({"inject_every": 0}, "inject_every"),
        ({"sample_rate": 0}, "sample_rate"),
        ({"bond": 0}, "the challenge bond must be positive"),
    ):
        with pytest.raises(quality.QualityEventError, match=message):
            quality.QualityAudit(
                client=client, params=protocol_params, profile=PROFILE, **overrides
            )

    defaults = quality.QualityAudit(client=client, params=protocol_params, profile=PROFILE)
    assert defaults.bond == max(1, protocol_params.min_bond)
    assert defaults.challenger == quality.challenger_address(protocol_params)
    assert (defaults.max_events, defaults.inject_every) == (
        quality.MAX_AUDIT_EVENTS,
        quality.INJECT_EVERY,
    )
    assert defaults.sample_rate == quality.QUALITY_SAMPLE_RATE
