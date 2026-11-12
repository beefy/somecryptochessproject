"""Deterministic quality sampling and the validator-side disagreement queue.

Block validity never depends on this module. It exists so a validator can answer
two questions cheaply:

1. *Should I analyse this proof?* With sampled quality a proof is selected by
   ``H(block_hash || proof_id) % 1000 < sample_rate``. Every validator computes the
   same bucket, so the same proofs are audited network-wide without coordination.
2. *What do I do with a disagreement?* Nothing automatic with an engine result:
   the result is queued, retried once, and reported. A node turns a persistent
   disagreement into a bonded challenge; the challenge, not the sampler, is what
   can cost anybody money.

The queue is deliberately synchronous. It is an *async job queue* in the sense
that :meth:`QualitySampler.run` processes a bounded number of jobs per call so a
node loop can interleave analysis with block handling; it never spawns a thread
or owns an engine.
"""

from __future__ import annotations

import hashlib
import json
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Literal

from sccp.chess.pgn import validate_pgn
from sccp.chess.quality import (
    QUALITY_V1,
    AnalysisCache,
    AnalysisEngine,
    QualityProfile,
    QualityReport,
    analyse_game,
    summary_matches,
)
from sccp.config import NetworkParams, QualityMode
from sccp.errors import QualityError
from sccp.protocol.challenges import MAX_EVIDENCE_LENGTH, MAX_REASON_LENGTH
from sccp.protocol.models import QualitySummary, WinProof

SAMPLE_BUCKETS: int = 1_000
"""The plan's sampling denominator."""

DEFAULT_MAX_ATTEMPTS: int = 2
"""One initial analysis plus one retry, matching the plan's retry requirement."""


def sample_bucket(block_hash: str, proof_id: str, *, buckets: int = SAMPLE_BUCKETS) -> int:
    """``H(candidate_block_hash || proof_id) mod buckets``.

    Both inputs are protocol hex digests. Hashing their *bytes* rather than their
    text avoids a leading-zero representation ambiguity between languages.
    """
    try:
        block = bytes.fromhex(block_hash)
        proof = bytes.fromhex(proof_id)
    except ValueError as exc:
        raise QualityError(f"sampling needs hex digests: {exc}") from None
    if not block or not proof:
        raise QualityError("sampling needs non-empty block and proof digests")
    if buckets < 1:
        raise QualityError("sample buckets must be positive")
    digest = hashlib.sha256(block + proof).digest()
    return int.from_bytes(digest[:8], "big") % buckets


def should_sample(
    block_hash: str,
    proof_id: str,
    *,
    sample_rate: int,
) -> bool:
    """Whether a proof falls in this validator's deterministic sample."""
    if not 0 <= sample_rate <= SAMPLE_BUCKETS:
        raise QualityError("sample_rate must be between 0 and 1000")
    return sample_bucket(block_hash, proof_id) < sample_rate


def select_samples(
    block_hash: str,
    proof_ids: Iterable[str],
    *,
    sample_rate: int,
) -> tuple[str, ...]:
    """The proof ids from ``proof_ids`` that this validator must analyse."""
    return tuple(
        proof_id
        for proof_id in proof_ids
        if should_sample(block_hash, proof_id, sample_rate=sample_rate)
    )


@dataclass(frozen=True, slots=True)
class SamplingDecision:
    """Why a proof was or was not selected, useful in logs and tests."""

    proof_id: str
    block_hash: str
    bucket: int
    sample_rate: int
    sampled: bool


def sampling_decision(
    block_hash: str,
    proof_id: str,
    *,
    sample_rate: int,
) -> SamplingDecision:
    """Return the full deterministic decision, not just its boolean."""
    bucket = sample_bucket(block_hash, proof_id)
    return SamplingDecision(
        proof_id=proof_id,
        block_hash=block_hash,
        bucket=bucket,
        sample_rate=sample_rate,
        sampled=bucket < sample_rate,
    )


@dataclass(frozen=True, slots=True)
class QualityJob:
    """One proof waiting for, or under, analysis."""

    block_hash: str
    proof: WinProof
    attempts: int = 0

    @property
    def proof_id(self) -> str:
        """The proof's protocol id."""
        return self.proof.proof_id


@dataclass(frozen=True, slots=True)
class SamplerChallenge:
    """Everything a node needs to build one bonded ``challenge`` transaction.

    The sampler never submits anything: it produces the reason and evidence, and
    the node decides the bond and pays the fee. Keeping the two apart is what
    makes a local engine crash harmless and a substantive disagreement explicit.
    """

    proof_id: str
    game_hash: str
    reason: str
    evidence: str

    def as_payload(self, *, bond: int, public_key: str) -> dict[str, object]:
        """The payload of a ``challenge`` transaction, with the node's bond."""
        return {
            "proof_id": self.proof_id,
            "reason": self.reason,
            "evidence": self.evidence,
            "bond": bond,
            "public_key": public_key,
        }


@dataclass(frozen=True, slots=True)
class SamplerResult:
    """The outcome of analysing one sampled proof.

    ``agrees`` is false for an engine failure too; ``error`` distinguishes a
    transport/engine problem from a substantive quality disagreement, so a node
    can avoid challenging a miner because its own engine crashed.
    """

    proof_id: str
    game_hash: str
    agrees: bool
    verdict: Literal["pass", "fail"] | None
    reasons: tuple[str, ...] = ()
    report: QualityReport | None = None
    attempts: int = 1
    error: str | None = None
    summary: QualitySummary | None = None
    """The summary the proof claimed, when it carried one."""

    @property
    def needs_challenge(self) -> bool:
        """Whether a persistent, substantive disagreement exists."""
        return not self.agrees and self.error is None

    @property
    def reason(self) -> str:
        """A challenge-sized explanation, bounded by ``MAX_REASON_LENGTH``."""
        text = self.reasons[0] if self.reasons else "quality re-run disagrees with the proof"
        if len(text) <= MAX_REASON_LENGTH:
            return text
        return text[: MAX_REASON_LENGTH - 3] + "..."

    @property
    def evidence(self) -> str:
        """Canonical JSON evidence for a challenge payload.

        Contains the claimed and observed trace hashes plus the disagreement
        reasons, which is what an adjudication panel needs to reproduce the
        finding without trusting the challenger's prose. The result is bounded by
        ``MAX_EVIDENCE_LENGTH``; a node must not submit evidence a block would
        reject.
        """
        document: dict[str, object] = {
            "proof_id": self.proof_id,
            "game_hash": self.game_hash,
            "verdict": self.verdict,
            "reasons": list(self.reasons[:5]),
            "policy": self.report.profile.policy if self.report is not None else None,
            "reported_trace_hash": self.summary.trace_hash if self.summary else None,
            "observed_trace_hash": self.report.trace_hash if self.report is not None else None,
            "engine_fingerprint": (
                self.report.engine_fingerprint if self.report is not None else None
            ),
        }
        text = json.dumps(document, sort_keys=True, separators=(",", ":"))
        if len(text) > MAX_EVIDENCE_LENGTH:
            raise QualityError(
                f"challenge evidence is {len(text)} characters, the limit is {MAX_EVIDENCE_LENGTH}"
            )
        return text

    def challenge(self) -> SamplerChallenge | None:
        """A challenge request, or ``None`` when there is nothing to challenge."""
        if not self.needs_challenge:
            return None
        return SamplerChallenge(
            proof_id=self.proof_id,
            game_hash=self.game_hash,
            reason=self.reason,
            evidence=self.evidence,
        )


@dataclass
class QualitySampler:
    """A bounded, deterministic sample queue for one validator.

    ``cache`` is shared with callers that analyse several games, which is common
    in a block with repeated openings. It is deterministic LRU, never time-based.
    """

    params: NetworkParams
    profile: QualityProfile = QUALITY_V1
    sample_rate: int | None = None
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    cache: AnalysisCache | None = None
    _queue: deque[QualityJob] = field(default_factory=deque, init=False, repr=False)
    _seen: set[str] = field(default_factory=set, init=False, repr=False)
    _results: dict[str, SamplerResult] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.sample_rate is None:
            self.sample_rate = self.params.quality_sample_rate
        if not 0 <= self.sample_rate <= SAMPLE_BUCKETS:
            raise QualityError("sample_rate must be between 0 and 1000")
        if self.max_attempts < 1:
            raise QualityError("max_attempts must be at least one")

    @property
    def required(self) -> bool:
        """Whether this network asks proofs to carry a summary at all."""
        return self.params.quality_mode is QualityMode.SAMPLED

    @property
    def pending(self) -> tuple[QualityJob, ...]:
        """Jobs still waiting, oldest first."""
        return tuple(self._queue)

    @property
    def results(self) -> dict[str, SamplerResult]:
        """Completed results keyed by proof id, in completion order."""
        return dict(self._results)

    def should_sample(self, block_hash: str, proof_id: str) -> bool:
        """Apply this sampler's rate to ``proof_id``."""
        assert self.sample_rate is not None
        return should_sample(block_hash, proof_id, sample_rate=self.sample_rate)

    def enqueue(self, block_hash: str, proof: WinProof) -> QualityJob | None:
        """Queue ``proof`` when sampled, deduplicating by proof id."""
        if not self.should_sample(block_hash, proof.proof_id):
            return None
        if proof.proof_id in self._seen:
            return None
        job = QualityJob(block_hash=block_hash, proof=proof)
        self._seen.add(proof.proof_id)
        self._queue.append(job)
        return job

    def enqueue_block(self, block_hash: str, proofs: Iterable[WinProof]) -> tuple[QualityJob, ...]:
        """Queue every sampled proof from a block, preserving wire order."""
        jobs = [self.enqueue(block_hash, proof) for proof in proofs]
        return tuple(job for job in jobs if job is not None)

    def result(self, proof_id: str) -> SamplerResult | None:
        """A completed result, or ``None`` while the proof is pending."""
        return self._results.get(proof_id)

    def analyse_proof(self, job: QualityJob, engine: AnalysisEngine) -> QualityReport:
        """Re-validate the proof's PGN and run the pinned profile over it."""
        proof = job.proof
        parsed = validate_pgn(proof.pgn, params=self.params, require_win=True)
        if parsed.game_hash != proof.game_hash:
            raise QualityError(
                f"proof {proof.proof_id} claims game {proof.game_hash}, PGN hashes to "
                f"{parsed.game_hash}"
            )
        if parsed.plies != proof.plies:
            raise QualityError(
                f"proof {proof.proof_id} claims {proof.plies} plies, PGN has {parsed.plies}"
            )
        return analyse_game(parsed, engine, profile=self.profile, cache=self.cache)

    def compare(
        self,
        job: QualityJob,
        report: QualityReport,
    ) -> tuple[bool, tuple[str, ...]]:
        """Compare a re-run report with the proof summary and the policy."""
        reasons: list[str] = []
        summary: QualitySummary | None = job.proof.quality_summary
        if summary is None:
            if self.required:
                reasons.append("sampled proof carries no quality summary")
        else:
            agrees, reason = summary_matches(summary, report)
            if not agrees:
                reasons.append(reason)
        if report.verdict == "fail":
            reasons.append("quality analysis verdict is 'fail'")
        return not reasons, tuple(reasons)

    def run(
        self,
        engine: AnalysisEngine,
        *,
        limit: int = 4,
    ) -> tuple[SamplerResult, ...]:
        """Process up to ``limit`` jobs and return the results produced now.

        A failed analysis is retried up to ``max_attempts`` before an ``error``
        result is recorded. Engine errors are never turned into a challenge,
        because the fault may be local.
        """
        if limit < 1:
            return ()
        produced: list[SamplerResult] = []
        for _ in range(limit):
            if not self._queue:
                break
            job = self._queue.popleft()
            try:
                report = self.analyse_proof(job, engine)
            except (QualityError, ValueError) as exc:
                attempts = job.attempts + 1
                if attempts < self.max_attempts:
                    self._queue.append(QualityJob(job.block_hash, job.proof, attempts))
                    continue
                result = SamplerResult(
                    proof_id=job.proof_id,
                    game_hash=job.proof.game_hash,
                    agrees=False,
                    verdict=None,
                    reasons=(f"analysis failed after {attempts} attempts: {exc}",),
                    attempts=attempts,
                    error=str(exc),
                    summary=job.proof.quality_summary,
                )
            else:
                agrees, reasons = self.compare(job, report)
                result = SamplerResult(
                    proof_id=job.proof_id,
                    game_hash=job.proof.game_hash,
                    agrees=agrees,
                    verdict=report.verdict if report.verdict in ("pass", "fail") else None,
                    reasons=reasons,
                    report=report,
                    attempts=job.attempts + 1,
                    summary=job.proof.quality_summary,
                )
            self._results[job.proof_id] = result
            produced.append(result)
        return tuple(produced)

    def disagreements(self) -> tuple[SamplerResult, ...]:
        """Substantive disagreements suitable for a bonded challenge."""
        return tuple(result for result in self._results.values() if result.needs_challenge)

    def challenge_requests(self) -> tuple[SamplerChallenge, ...]:
        """Challenge payloads for every disagreement, in deterministic order.

        Sorted by ``(game_hash, proof_id)`` so a node that processes two blocks in
        a different order still submits the same set of challenges. An engine
        failure is excluded by :attr:`SamplerResult.needs_challenge`.
        """
        requests = [result.challenge() for result in self.disagreements()]
        return tuple(
            sorted(
                (request for request in requests if request is not None),
                key=lambda request: (request.game_hash, request.proof_id),
            )
        )


__all__ = [
    "DEFAULT_MAX_ATTEMPTS",
    "SAMPLE_BUCKETS",
    "QualityJob",
    "QualitySampler",
    "SamplerChallenge",
    "SamplerResult",
    "SamplingDecision",
    "sample_bucket",
    "sampling_decision",
    "select_samples",
    "should_sample",
]
