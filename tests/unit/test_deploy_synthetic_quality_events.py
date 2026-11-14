"""The synthetic quality auditor: re-run a proof, accuse it, report the verdict.

``deploy/synthetic_quality_events.py`` drives the real path: it reads proofs that
carry a quality summary, re-runs the node's own sampler over them, signs a bonded
``challenge`` transaction when the summary is false, and reports what the chain's
panel decided. These tests pin it with a uniform engine (agrees), a losing engine
(disagrees), a crashing engine (fails) and a fake node, so no Stockfish process
starts and no socket opens.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator, Mapping, Sequence

import chess
import httpx
import pytest
from deploy import synthetic_quality_events as quality

from sccp.chess.pgn import validate_pgn
from sccp.chess.quality import QUALITY_V1, QualityProfile, analyse_game
from sccp.config import NetworkParams
from sccp.crypto.address import validate_address
from sccp.errors import QualityError
from sccp.protocol.models import QualitySummary, WinProof

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
    """Every position is equal, so a clean game analyses clean."""

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


def _honest_summary(proof: WinProof, params: NetworkParams) -> QualitySummary:
    """What an honest miner running ``_UniformEngine`` would report for ``proof``."""
    parsed = validate_pgn(proof.pgn, params=params, require_win=True)
    return analyse_game(parsed, _UniformEngine(), profile=PROFILE).summary()


def _summarised(proof: WinProof, summary: QualitySummary) -> WinProof:
    """``proof`` carrying ``summary``: the shape a miner's claim arrives in."""
    return proof.model_copy(update={"quality_summary": summary})


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
    """The slice of the node's HTTP API a quality audit reads and writes."""

    def __init__(
        self,
        *,
        tip: int = 5,
        blocks: Mapping[int, Mapping[str, object]] | None = None,
        balance: int = 10**18,
        nonce: int = 0,
        challenges: Sequence[Mapping[str, object]] = (),
        reject_post: bool = False,
        status_status: int = 200,
        blocks_status: int = 200,
    ) -> None:
        self.tip = tip
        self.blocks = dict(blocks or {})
        self.balance = balance
        self.nonce = nonce
        self.challenges = list(challenges)
        self.reject_post = reject_post
        self.status_status = status_status
        self.blocks_status = blocks_status
        self.calls: list[str] = []
        self.posted: list[Mapping[str, object]] = []
        self.client = httpx.Client(transport=httpx.MockTransport(self._handle), base_url=NODE)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(f"{request.method} {path}")
        if request.method == "POST":
            if path != "/v1/tx":
                raise AssertionError(f"unexpected POST {request.url}")
            if self.reject_post:
                return httpx.Response(400, json={"detail": "refused"})
            self.posted.append(json.loads(request.content))
            return httpx.Response(202, json={"network": "devnet", "txid": "00" * 32})
        if path == "/v1/status":
            if self.status_status != 200:
                return httpx.Response(self.status_status, json={"error": "unavailable"})
            return httpx.Response(200, json={"network": "devnet", "height": self.tip})
        if path == "/v1/challenges":
            return httpx.Response(200, json={"network": "devnet", "challenges": self.challenges})
        if path.startswith("/v1/accounts/"):
            return httpx.Response(
                200,
                json={
                    "network": "devnet",
                    "address": path.rsplit("/", 1)[-1],
                    "balance": self.balance,
                    "nonce": self.nonce,
                },
            )
        if path.startswith("/v1/blocks/"):
            height = int(path.rsplit("/", 1)[-1])
            if self.blocks_status != 200:
                return httpx.Response(self.blocks_status, json={"error": "no such block"})
            body = self.blocks.get(height)
            return httpx.Response(
                200,
                json=dict(body) if body is not None else _block(height, proofs=(), miner="empty"),
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


# -- the challenger account ----------------------------------------------------


def test_the_challenge_envelope_passes_the_consensus_rules() -> None:
    """The ticket the audit files is a real transaction, checked by the chain code."""
    from sccp.chain.state import ProofRecord, State
    from sccp.chain.tx import apply_transaction, validate_transaction
    from sccp.config import DEVNET

    keypair = quality.challenger_keypair()
    challenger = quality.challenger_address(DEVNET)
    state = State(params=DEVNET, height=1, timestamps=[1_700_000_000])
    state.proofs["ee" * 32] = ProofRecord(
        proof_id="ab" * 32,
        game_hash="ee" * 32,
        height=1,
        winner="sccpt1winnerwinnerwinner",
        miner="sccpt1minerminerminer",
    )
    state.proof_ids["ab" * 32] = "ee" * 32
    state.ensure(challenger).balance = 10**15

    tx = quality.challenge_transaction(
        keypair=keypair,
        challenger=challenger,
        params=DEVNET,
        proof_id="ab" * 32,
        reason="quality summary does not match an independent re-run",
        evidence='{"kind":"quality-audit"}',
        bond=max(1, DEVNET.min_bond),
        nonce=0,
    )
    validate_transaction(tx, DEVNET, state, height=2)
    apply_transaction(state, tx, height=2)

    (record,) = state.challenges.values()
    assert (record.challenger, record.proof_id, record.status) == (challenger, "ab" * 32, "open")
    assert state.ensure(challenger).balance == 10**15 - max(1, DEVNET.min_bond) - DEVNET.min_fee


def test_the_challenger_account_is_derived_from_a_published_seed(
    protocol_params: NetworkParams,
) -> None:
    address = quality.challenger_address(protocol_params)
    assert address == quality.challenger_address(protocol_params)
    assert validate_address(address, protocol_params)
    assert address != quality.challenger_address(
        protocol_params.model_copy(update={"address_hrp": "sccp"})
    )

    audit = quality.QualityAudit(params=protocol_params, submit=True, client=_Node().client)
    assert audit.challenger == address
    assert audit.bond == max(1, protocol_params.min_bond)
    with pytest.raises(quality.QualityEventError, match="no other account can sign"):
        quality.QualityAudit(
            params=protocol_params,
            submit=True,
            challenger="sccpt1somebodyelse",
            client=_Node().client,
        )


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


# -- what an audit does with a summary -----------------------------------------


def test_a_proof_without_a_summary_is_left_alone(
    node, signed_match, protocol_params: NetworkParams
) -> None:
    """Every honest testnet proof looks like this; accusing one would be wrong."""
    fake = node(tip=5, blocks={5: _block(5, proofs=[signed_match.proof], miner="sccp1m")})
    report = _audit(fake, protocol_params, engine=_UniformEngine()).audit()

    assert report.events == ()
    assert report.challenged == 0
    assert "no proof carrying a summary" in report.blocked
    assert fake.posted == []


def test_an_honest_summary_is_reported_and_not_challenged(
    node, signed_match, protocol_params: NetworkParams
) -> None:
    proof = _summarised(signed_match.proof, _honest_summary(signed_match.proof, protocol_params))
    fake = node(tip=5, blocks={5: _block(5, proofs=[proof], miner="sccp1miner")})

    report = _audit(fake, protocol_params, engine=_UniformEngine()).audit()

    event = report.events[0]
    assert (event.height, event.proof_id, event.miner) == (5, proof.proof_id, "sccp1miner")
    assert event.verdict == "pass"
    assert event.reasons == ()
    assert not event.challenged
    assert "summary agrees" in event.note
    assert fake.posted == []
    assert report.as_line().startswith("quality: block 5")


def test_a_false_summary_files_a_signed_bonded_challenge(
    node, signed_match, protocol_params: NetworkParams
) -> None:
    """The miner claims a clean game; the honest re-run finds blunders."""
    claim = _honest_summary(signed_match.proof, protocol_params)
    proof = _summarised(signed_match.proof, claim)
    fake = node(tip=5, blocks={5: _block(5, proofs=[proof], miner="sccp1miner")})

    audit = _audit(fake, protocol_params, engine=_LosingEngine(), submit=True)
    report = audit.audit()

    event = report.events[0]
    assert event.verdict == "fail"
    assert event.reasons
    assert event.submitted
    assert event.challenged
    assert report.challenged == 1
    assert "challenge submitted" in event.as_line()

    (posted,) = fake.posted
    assert posted["type"] == "challenge"
    assert posted["from"] == audit.challenger
    assert posted["nonce"] == 0
    assert posted["fee"] == protocol_params.min_fee
    assert posted["signature"]
    payload = posted["payload"]
    assert payload["proof_id"] == proof.proof_id
    assert payload["bond"] == audit.bond
    assert payload["public_key"] == quality.challenger_keypair().public_key_hex
    evidence = json.loads(payload["evidence"])
    assert evidence["kind"] == "quality-audit"
    assert evidence["claimed"]["verdict"] == claim.verdict
    assert evidence["observed_verdict"] == "fail"
    assert "does not match" in payload["reason"]


# -- what the chain decided ----------------------------------------------------


def test_the_audit_reports_the_verdicts_the_panel_reached(
    node, protocol_params: NetworkParams
) -> None:
    address = quality.challenger_address(protocol_params)
    fake = node(
        tip=5,
        challenges=[
            {
                "challenge_id": "aa" * 32,
                "challenger": address,
                "proof_id": "01" * 32,
                "status": "upheld",
                "bond": 7,
            },
            {
                "challenge_id": "bb" * 32,
                "challenger": address,
                "proof_id": "02" * 32,
                "status": "rejected",
                "bond": 7,
            },
            {  # somebody else's case: not this auditor's to report
                "challenge_id": "cc" * 32,
                "challenger": "sccpt1anotherchallenger",
                "proof_id": "03" * 32,
                "status": "upheld",
                "bond": 7,
            },
            {  # still waiting for the panel
                "challenge_id": "dd" * 32,
                "challenger": address,
                "proof_id": "04" * 32,
                "status": "open",
                "bond": 7,
            },
        ],
    )
    audit = _audit(fake, protocol_params, engine=_UniformEngine(), submit=True)

    report = audit.audit()
    assert [settlement.status for settlement in report.settlements] == ["upheld", "rejected"]
    assert report.upheld == 1
    assert report.rejected == 1
    upheld = report.settlements[0]
    assert (upheld.proof_id, upheld.bond, upheld.slashed, upheld.survived) == (
        "01" * 32,
        7,
        True,
        False,
    )
    assert "miner slashed" in upheld.as_line()
    assert "bond burned" in report.settlements[1].as_line()
    assert "challenge aa" in report.as_line() or "challenge aaaa" in report.as_line()

    # A verdict is reported once, not on every later cycle.
    assert audit.audit().settlements == ()


def test_an_open_challenge_with_no_adjudicator_is_called_out(
    node, protocol_params: NetworkParams
) -> None:
    """A bond locked behind an empty panel must not look like a case in progress."""
    fake = node(
        tip=5,
        challenges=[
            {
                "challenge_id": "ee" * 32,
                "challenger": quality.challenger_address(protocol_params),
                "proof_id": "05" * 32,
                "status": "open",
                "adjudicators": [],
                "bond": 7,
            }
        ],
    )
    report = _audit(fake, protocol_params, engine=_UniformEngine(), submit=True).audit()

    assert report.stranded == ("ee" * 32,)
    assert "no bonded adjudicator" in report.as_line()
    assert "ee" * 5 in report.as_line()


def test_a_short_challenger_says_so_and_files_nothing(
    node, signed_match, protocol_params: NetworkParams
) -> None:
    claim = _honest_summary(signed_match.proof, protocol_params)
    fake = node(
        tip=5,
        balance=5,
        blocks={5: _block(5, proofs=[_summarised(signed_match.proof, claim)], miner="sccp1m")},
    )
    report = _audit(fake, protocol_params, engine=_LosingEngine(), submit=True).audit()

    event = report.events[0]
    assert not event.submitted
    assert "holds 5 units" in event.note
    assert "top-up has not landed" in event.note
    assert fake.posted == []


def test_a_refused_submission_is_a_note_and_the_next_nonce_is_reread(
    node, signed_match, protocol_params: NetworkParams
) -> None:
    claim = _honest_summary(signed_match.proof, protocol_params)
    fake = node(
        tip=5,
        reject_post=True,
        blocks={5: _block(5, proofs=[_summarised(signed_match.proof, claim)], miner="sccp1m")},
    )
    audit = _audit(fake, protocol_params, engine=_LosingEngine(), submit=True)
    report = audit.audit()

    event = report.events[0]
    assert not event.submitted
    assert "refused the challenge" in event.note
    assert audit._nonce is None


def test_a_deliberate_false_challenge_is_filed_on_request(
    node, signed_match, protocol_params: NetworkParams
) -> None:
    claim = _honest_summary(signed_match.proof, protocol_params)
    fake = node(
        tip=5,
        blocks={5: _block(5, proofs=[_summarised(signed_match.proof, claim)], miner="sccp1m")},
    )

    audit = _audit(
        fake,
        protocol_params,
        engine=_UniformEngine(),
        submit=True,
        false_challenge_every=1,
    )
    report = audit.audit()

    event = report.events[0]
    assert event.submitted  # the summary agreed; the accusation is knowingly wrong
    (posted,) = fake.posted
    assert posted["payload"]["reason"] == quality.FALSE_CHALLENGE_REASON.strip()[:120]


def test_a_read_only_audit_spends_nothing(
    node, signed_match, protocol_params: NetworkParams
) -> None:
    claim = _honest_summary(signed_match.proof, protocol_params)
    fake = node(
        tip=5,
        blocks={5: _block(5, proofs=[_summarised(signed_match.proof, claim)], miner="sccp1m")},
    )
    report = _audit(fake, protocol_params, engine=_LosingEngine(), submit=False).audit()

    event = report.events[0]
    assert not event.challenged
    assert "read-only audit" in event.note
    assert fake.posted == []
    assert report.settlements == ()


# -- failures and lifecycle ----------------------------------------------------


def test_an_engine_failure_is_reported_rather_than_challenged(
    node, signed_match, protocol_params: NetworkParams
) -> None:
    claim = _honest_summary(signed_match.proof, protocol_params)
    fake = node(
        tip=5,
        blocks={5: _block(5, proofs=[_summarised(signed_match.proof, claim)], miner="sccp1m")},
    )
    engine = _BrokenEngine()
    report = _audit(fake, protocol_params, engine=engine, submit=True).audit()

    event = report.events[0]
    assert engine.attempts == 2  # the sampler's own retry budget, not a single try
    assert event.error == "engine crashed"
    assert event.reasons == ("analysis failed after 2 attempts: engine crashed",)
    assert event.verdict is None
    assert not event.challenged
    assert report.challenged == 0
    assert fake.posted == []
    assert event.as_line().endswith("analysis failed: engine crashed")


def test_the_audit_follows_the_chain_forward(
    node, signed_match, protocol_params: NetworkParams
) -> None:
    claim = _honest_summary(signed_match.proof, protocol_params)
    first = _summarised(signed_match.proof.model_copy(update={"proof_id": "01" * 32}), claim)
    second = _summarised(signed_match.proof.model_copy(update={"proof_id": "02" * 32}), claim)
    fake = node(
        tip=6,
        blocks={
            5: _block(5, proofs=[first], miner="sccp1miner"),
            6: _block(6, proofs=[second], miner="sccp1miner"),
        },
    )
    audit = _audit(fake, protocol_params, engine=_UniformEngine(), max_events=2)

    report = audit.audit()
    assert [event.height for event in report.events] == [5, 6]
    assert report.analysed == 2

    again = audit.audit()
    assert again.events == ()
    assert again.blocked == "no block after 6"
    assert again.as_line().startswith("quality: no block after 6")


def test_a_window_without_a_proof_says_so(node, protocol_params: NetworkParams) -> None:
    fake = node(tip=4)
    audit = _audit(fake, protocol_params, engine=_UniformEngine())

    report = audit.audit()
    assert report.events == ()
    assert report.challenged == 0
    assert "no proof carrying a summary between blocks 1 and 4" in report.blocked
    assert report.as_line() == "quality: " + report.blocked


def test_a_network_without_a_challenge_window_skips_the_audit(
    node, protocol_params: NetworkParams
) -> None:
    fake = node(tip=5)
    blocked = _audit(
        fake,
        protocol_params.model_copy(update={"challenge_window": 0}),
        engine=_UniformEngine(),
    ).audit()

    assert blocked.events == ()
    assert "disables quality challenges" in blocked.blocked
    assert fake.calls == []


def test_an_owned_engine_is_opened_lazily_and_closed_once(
    node,
    signed_match,
    protocol_params: NetworkParams,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    opened: list[str] = []

    class _Engine(_UniformEngine):
        def __init__(self) -> None:
            self.closed = 0

        def close(self) -> None:
            self.closed += 1

    engine = _Engine()
    binary = tmp_path / "stockfish"
    binary.write_text("", encoding="utf-8")

    def factory(path, profile: str) -> _Engine:
        opened.append(str(path))
        return engine

    monkeypatch.setattr(quality, "open_analysis_engine", factory)
    claim = _honest_summary(signed_match.proof, protocol_params)
    fake = node(
        tip=5,
        blocks={5: _block(5, proofs=[_summarised(signed_match.proof, claim)], miner="sccp1m")},
    )
    audit = quality.QualityAudit(
        client=fake.client,
        params=protocol_params,
        profile=PROFILE,
        engine_path=binary,
    )

    audit.audit()
    assert opened == [str(binary)]
    audit.close()
    audit.close()
    assert engine.closed == 1


def test_a_missing_engine_binary_is_one_clear_error(
    node, signed_match, protocol_params: NetworkParams, tmp_path
) -> None:
    claim = _honest_summary(signed_match.proof, protocol_params)
    fake = node(
        tip=5,
        blocks={5: _block(5, proofs=[_summarised(signed_match.proof, claim)], miner="sccp1m")},
    )
    audit = quality.QualityAudit(
        client=fake.client,
        params=protocol_params,
        profile=PROFILE,
        engine_path=tmp_path / "missing-stockfish",
    )

    with pytest.raises(quality.QualityEventError, match="no engine binary"):
        audit.audit()


def test_a_node_that_cannot_be_read_is_a_cycle_failure(
    node, protocol_params: NetworkParams
) -> None:
    fake = node(status_status=503)
    audit = _audit(fake, protocol_params, engine=_UniformEngine())

    with pytest.raises(quality.QualityEventError, match="GET /v1/status failed: 503"):
        audit.audit()


def test_a_block_that_cannot_be_read_is_a_cycle_failure(
    node, protocol_params: NetworkParams
) -> None:
    fake = node(tip=5, blocks_status=500)
    audit = _audit(fake, protocol_params, engine=_UniformEngine())

    with pytest.raises(quality.QualityEventError, match="GET /v1/blocks/1 failed: 500"):
        audit.audit()


def test_the_constructor_refuses_unusable_budgets(node, protocol_params: NetworkParams) -> None:
    fake = node(tip=1)
    with pytest.raises(quality.QualityEventError, match="max_events"):
        _audit(fake, protocol_params, engine=_UniformEngine(), max_events=0)
    with pytest.raises(quality.QualityEventError, match="sample_rate"):
        _audit(fake, protocol_params, engine=_UniformEngine(), sample_rate=0)
    with pytest.raises(quality.QualityEventError, match="false_challenge_every"):
        _audit(fake, protocol_params, engine=_UniformEngine(), false_challenge_every=-1)
    with pytest.raises(quality.QualityEventError, match="bond"):
        _audit(fake, protocol_params, engine=_UniformEngine(), bond=0)


def test_a_closed_auditor_refuses_to_work(node, protocol_params: NetworkParams) -> None:
    fake = node(tip=1)
    audit = _audit(fake, protocol_params, engine=_UniformEngine())
    audit.close()
    with pytest.raises(quality.QualityEventError, match="closed"):
        audit.audit()
