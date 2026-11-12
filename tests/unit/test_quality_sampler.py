"""Deterministic quality sampling and the validator disagreement queue (M5)."""

from __future__ import annotations

import chess
import pytest

from sccp.chess.pgn import validate_pgn
from sccp.chess.quality import QUALITY_V1, QualityProfile, analyse_game
from sccp.config import NetworkParams, QualityMode
from sccp.errors import QualityError
from sccp.node.quality_sampler import (
    QualityJob,
    QualitySampler,
    sample_bucket,
    select_samples,
    should_sample,
)
from sccp.protocol.models import WinProof

PROFILE = QualityProfile(
    policy=QUALITY_V1.policy,
    engine=QUALITY_V1.engine,
    depth=1,
    max_cpl=0,
    decided_cp=0,
    max_blunders_per_side=1_000,
    resign_cp=300,
    min_plies=1,
)


class _UniformEngine:
    """A deterministic zero-centipawn engine, enough for the policy plumbing."""

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
        move = legal[0]
        return [
            {
                "score": chess.engine.PovScore(chess.engine.Cp(0), board.turn),
                "pv": [move],
            }
        ]


class _BrokenEngine:
    name = "broken"
    fingerprint = "broken"

    def analyse(self, board, limit, *, multipv=1):
        raise QualityError("engine crashed")


def _sampled(params: NetworkParams, rate: int = 1_000) -> NetworkParams:
    return params.model_copy(
        update={"quality_mode": QualityMode.SAMPLED, "quality_sample_rate": rate}
    )


def _report(proof: WinProof, params: NetworkParams, engine=None):
    parsed = validate_pgn(proof.pgn, params=params, require_win=True)
    return analyse_game(parsed, engine or _UniformEngine(), profile=PROFILE)


def test_bucket_is_deterministic_and_rate_bounded() -> None:
    block = "11" * 32
    proof = "22" * 32
    first = sample_bucket(block, proof)
    assert first == sample_bucket(block, proof)
    assert 0 <= first < 1_000
    assert 0 <= sample_bucket(block, proof, buckets=7) < 7
    assert sample_bucket(block, proof, buckets=7) == sample_bucket(block, proof, buckets=7)
    assert should_sample(block, proof, sample_rate=1_000)
    assert not should_sample(block, proof, sample_rate=0)
    assert should_sample(block, proof, sample_rate=first + 1)
    assert not should_sample(block, proof, sample_rate=first)


def test_bucket_rejects_malformed_input() -> None:
    with pytest.raises(QualityError):
        sample_bucket("not-hex", "22" * 32)
    with pytest.raises(QualityError):
        sample_bucket("", "22" * 32)
    with pytest.raises(QualityError):
        sample_bucket("11" * 32, "22" * 32, buckets=0)
    with pytest.raises(QualityError):
        should_sample("11" * 32, "22" * 32, sample_rate=1_001)


def test_select_samples_preserves_order() -> None:
    block = "11" * 32
    proofs = ["01" * 32, "02" * 32, "03" * 32, "04" * 32]
    selected = select_samples(block, proofs, sample_rate=1_000)
    assert selected == tuple(proofs)
    assert select_samples(block, proofs, sample_rate=0) == ()



def test_enqueue_is_sampled_and_deduplicated(signed_match, protocol_params) -> None:
    params = _sampled(protocol_params)
    sampler = QualitySampler(params=params, profile=PROFILE)
    proof = signed_match.proof

    job = sampler.enqueue("11" * 32, proof)
    assert job is not None
    assert job.proof_id == proof.proof_id
    assert sampler.enqueue("11" * 32, proof) is None
    assert [pending.proof_id for pending in sampler.pending] == [proof.proof_id]


def test_unsampled_proofs_never_enter_the_queue(signed_match, protocol_params) -> None:
    sampler = QualitySampler(params=_sampled(protocol_params, 0), profile=PROFILE)
    assert sampler.enqueue("11" * 32, signed_match.proof) is None
    assert sampler.pending == ()


def test_agreement_with_the_summary_is_not_a_disagreement(
    signed_match, protocol_params
) -> None:
    params = _sampled(protocol_params)
    report = _report(signed_match.proof, params)
    proof = signed_match.proof.model_copy(update={"quality_summary": report.summary()})
    sampler = QualitySampler(params=params, profile=PROFILE)
    sampler.enqueue("11" * 32, proof)

    results = sampler.run(_UniformEngine())

    assert len(results) == 1
    result = results[0]
    assert result.agrees
    assert result.needs_challenge is False
    assert result.verdict == "pass"
    assert result.reasons == ()
    assert result.attempts == 1
    assert sampler.result(proof.proof_id) is result
    assert sampler.disagreements() == ()


def test_missing_summary_is_a_substantive_disagreement(
    signed_match, protocol_params
) -> None:
    params = _sampled(protocol_params)
    sampler = QualitySampler(params=params, profile=PROFILE)
    sampler.enqueue("11" * 32, signed_match.proof)

    (result,) = sampler.run(_UniformEngine())

    assert result.agrees is False
    assert result.error is None
    assert result.needs_challenge is True
    assert "no quality summary" in result.reasons[0]
    assert sampler.disagreements() == (result,)


def test_mismatched_counters_are_reported(signed_match, protocol_params) -> None:
    params = _sampled(protocol_params)
    report = _report(signed_match.proof, params)
    summary = report.summary().model_copy(update={"black_blunders": 9})
    proof = signed_match.proof.model_copy(update={"quality_summary": summary})
    sampler = QualitySampler(params=params, profile=PROFILE)
    sampler.enqueue("11" * 32, proof)

    (result,) = sampler.run(_UniformEngine())

    assert result.agrees is False
    assert result.needs_challenge is True
    assert any("black blunders" in reason for reason in result.reasons)


def test_missing_summary_is_fine_when_quality_is_off(signed_match, protocol_params) -> None:
    sampler = QualitySampler(params=protocol_params, profile=PROFILE)
    assert sampler.required is False
    report = _report(signed_match.proof, protocol_params)
    job = QualityJob(block_hash="11" * 32, proof=signed_match.proof)

    agrees, reasons = sampler.compare(job, report)

    assert agrees is True
    assert reasons == ()


def test_engine_failure_is_retried_and_never_challenges(
    signed_match, protocol_params
) -> None:
    params = _sampled(protocol_params)
    sampler = QualitySampler(params=params, profile=PROFILE, max_attempts=2)
    sampler.enqueue("11" * 32, signed_match.proof)

    assert sampler.run(_BrokenEngine(), limit=1) == ()
    (retry,) = sampler.pending
    assert retry.attempts == 1

    (result,) = sampler.run(_BrokenEngine())
    assert result.agrees is False
    assert result.error is not None
    assert result.needs_challenge is False
    assert sampler.disagreements() == ()


def test_run_limit_bounds_work_per_call(signed_match, protocol_params) -> None:
    params = _sampled(protocol_params)
    sampler = QualitySampler(params=params, profile=PROFILE)
    proof = signed_match.proof
    sampler.enqueue("11" * 32, proof.model_copy(update={"proof_id": "01" * 32}))
    sampler.enqueue("11" * 32, proof.model_copy(update={"proof_id": "02" * 32}))

    first = sampler.run(_UniformEngine(), limit=1)
    assert len(first) == 1
    assert len(sampler.pending) == 1
    assert sampler.run(_UniformEngine(), limit=0) == ()
    second = sampler.run(_UniformEngine(), limit=1)
    assert len(second) == 1
    assert sampler.pending == ()


def test_analyse_proof_rejects_a_pgn_that_does_not_match_the_claim(
    signed_match, protocol_params
) -> None:
    params = _sampled(protocol_params)
    sampler = QualitySampler(params=params, profile=PROFILE)
    bad = signed_match.proof.model_copy(update={"game_hash": "00" * 32})
    job = sampler.enqueue("11" * 32, bad)
    assert job is not None
    with pytest.raises(QualityError, match="hashes to"):
        sampler.analyse_proof(job, _UniformEngine())
