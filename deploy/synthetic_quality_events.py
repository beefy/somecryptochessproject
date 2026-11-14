"""Deterministic quality events: re-run the pinned profile, then replay the challenge.

The node re-runs ``quality-v1`` over sampled proofs and opens a bonded challenge
when the re-run disagrees with the summary the proof carried
(``sccp.node.quality_sampler``). Testnet leaves ``quality_mode`` off, so no proof
carries a summary and no challenge is ever opened: the whole path from "a proof
was accepted" to "a bond moved" is dead code in a hosted deployment.

This module exercises that path from outside the node, at a pace an operator can
watch:

* it reads the newest blocks from ``GET /v1/blocks/{height}`` and runs the node's
  own :class:`~sccp.node.quality_sampler.QualitySampler` over their proofs, so the
  sampling bucket, the PGN re-validation and the summary comparison are the
  node's code rather than a copy of it;
* every second event offers the sampler the *same* proof with a fabricated
  summary -- a clean game with a trace hash that is not the one the re-run
  produces -- which is the shape of a miner lying about its evidence;
* it replays ``open_challenge``, ``challenge_panel`` and ``adjudicate_with_panel``
  in a local :class:`~sccp.chain.state.State` seeded from the node's roster, so
  the bond accounting, the beacon-ranked panel and the slash are the chain's rules.

Nothing is submitted: the mirror lives in this process, so a synthetic challenge
can never invalidate a real proof. The report says what *would* have happened --
``upheld`` (the miner's bond would be slashed) or ``rejected`` (the challenger's
bond would be burned).
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

import httpx

from sccp.chain.state import EpochRecord, ProofRecord, State
from sccp.chess.engines import open_engine
from sccp.chess.quality import QUALITY_V1, AnalysisCache, AnalysisEngine, QualityProfile
from sccp.config import TESTNET, NetworkParams
from sccp.crypto.address import encode_address
from sccp.crypto.pq import hybrid_keypair_from_seed
from sccp.errors import ChallengeError, EngineError, QualityError
from sccp.node.quality_sampler import (
    SAMPLE_BUCKETS,
    QualitySampler,
    SamplerResult,
    SamplingDecision,
    sampling_decision,
)
from sccp.protocol import challenges
from sccp.protocol.models import Beacon, QualitySummary, RosterMember, WinProof

QUALITY_SAMPLE_RATE: Final = SAMPLE_BUCKETS
"""How often the auditor looks at a proof.

The node samples at ``quality_sample_rate``, which is ``0`` on testnet because
quality enforcement is off there. The auditor enforces nothing on the chain, and
a block carries one or two proofs a minute, so it accepts every candidate and
lets ``max_events`` do the rate limiting.
"""

MAX_AUDIT_EVENTS: Final = 1
"""How many proofs one audit may analyse.

A depth-14 multi-PV re-run of a 60-ply game costs about what a match costs, so
one per cycle keeps the generator background load rather than the main load.
"""

INJECT_EVERY: Final = 2
"""Every second event offers the sampler a proof with a fabricated summary."""

CHALLENGER_NAMESPACE: Final = b"sccp/testnet/quality-challenger/"
"""The published seed of the account whose challenges are replayed."""

ENGINE_PROFILE: Final = "stockfish-19"
"""The engine profile the miners register, so the re-run fingerprints match."""

ENGINE_PATH: Final = Path("/usr/games/stockfish")
"""The binary the stack installs; the miners play with this same file."""

_PANEL_SIZES: Final = (challenges.PANEL_SIZE, 1)
"""Panel sizes to try, largest first. A testnet roster may bond fewer than three."""


class QualityEventError(RuntimeError):
    """An audit could not read the node or seat a challenge."""


Challenger = str


def challenger_address(params: NetworkParams = TESTNET) -> str:
    """The deterministic account that would open the synthetic challenges.

    Its seed is published, like the traffic wallets', so an operator can sign the
    same challenge for real if they want the node to admit it.
    """
    keypair = hybrid_keypair_from_seed(hashlib.sha256(CHALLENGER_NAMESPACE).digest())
    return encode_address(keypair.public_key_bytes, params)


def fabricate_summary(profile: QualityProfile = QUALITY_V1) -> QualitySummary:
    """A summary that claims a clean game and commits to no trace.

    Only the policy id has to be real: the node's comparison reports the trace
    hash mismatch, which is exactly the finding a lying miner produces.
    """
    return QualitySummary(
        policy=profile.policy,
        verdict="pass",
        trace_hash="00" * 32,
        white_blunders=0,
        black_blunders=0,
        worst_cpl=0,
    )


def lying_proof(proof: WinProof, profile: QualityProfile = QUALITY_V1) -> WinProof:
    """``proof`` with a fabricated summary attached; the proof id is unchanged."""
    return proof.model_copy(update={"quality_summary": fabricate_summary(profile)})


def open_analysis_engine(
    path: str | Path = ENGINE_PATH,
    profile: str = ENGINE_PROFILE,
) -> AnalysisEngine:
    """Open the engine ``sccp miner run`` uses, so the fingerprints line up."""
    return open_engine(profile, binary=Path(path))


# -- talking to the node ------------------------------------------------------


def _get_object(client: httpx.Client, path: str) -> Mapping[str, object]:
    """GET ``path`` and return a JSON object, or raise :class:`QualityEventError`."""
    response = client.get(path)
    if response.status_code >= 300:
        raise QualityEventError(f"GET {path} failed: {response.status_code}")
    return _as_object(response, path)


def _get_optional(client: httpx.Client, path: str) -> Mapping[str, object] | None:
    """GET ``path``, treating a 404 as "the chain has not opened it yet"."""
    response = client.get(path)
    if response.status_code == 404:
        return None
    if response.status_code >= 300:
        raise QualityEventError(f"GET {path} failed: {response.status_code}")
    return _as_object(response, path)


def _as_object(response: httpx.Response, path: str) -> Mapping[str, object]:
    """The response body, when it is a JSON object."""
    payload = response.json()
    if not isinstance(payload, Mapping):
        raise QualityEventError(f"GET {path} did not return an object")
    return payload


def tip_height(client: httpx.Client) -> int:
    """The node's tip height."""
    view = _get_object(client, "/v1/status")
    height = view.get("height")
    if not isinstance(height, int) or isinstance(height, bool) or height < 0:
        raise QualityEventError("GET /v1/status did not report a height")
    return height


def _digest(value: object, field_name: str, context: str) -> str:
    """A non-empty hex digest from a view, or :class:`QualityEventError`."""
    if not isinstance(value, str) or not value:
        raise QualityEventError(f"{context} has no {field_name}")
    return value


def block_proofs(view: Mapping[str, object], height: int) -> tuple[WinProof, ...]:
    """The proofs a ``GET /v1/blocks/{height}`` body carries, in wire order."""
    block = view.get("block")
    if not isinstance(block, Mapping):
        raise QualityEventError(f"block {height} has no body")
    raw = block.get("proofs") or ()
    if not isinstance(raw, (list, tuple)):
        raise QualityEventError(f"block {height} has an unreadable proof list")
    proofs: list[WinProof] = []
    for entry in raw:
        try:
            proofs.append(WinProof.model_validate(entry))
        except ValueError as exc:
            raise QualityEventError(f"block {height} carries an unreadable proof: {exc}") from None
    return tuple(proofs)


def block_miner(view: Mapping[str, object], height: int) -> str:
    """The ``miner`` of the block's sealed header."""
    block = view.get("block")
    header = block.get("header") if isinstance(block, Mapping) else None
    miner = header.get("miner") if isinstance(header, Mapping) else None
    if not isinstance(miner, str) or not miner:
        raise QualityEventError(f"block {height} has no miner")
    return miner


# -- what an audit produces ---------------------------------------------------


@dataclass(frozen=True, slots=True)
class Resolution:
    """What the replayed challenge would have done to the two bonds."""

    challenge_id: str
    status: str
    """``upheld`` or ``rejected``."""
    panel: tuple[str, ...]
    bond: int
    returned: int
    burned: int
    slashed: bool

    @property
    def survived(self) -> bool:
        """Whether the proof the challenge named is still valid."""
        return self.status != "upheld"


@dataclass(frozen=True, slots=True)
class QualityEvent:
    """One analysed proof and the challenge it did or did not warrant."""

    height: int
    proof_id: str
    game_hash: str
    miner: str
    origin: str
    """``sampled`` (the proof as the chain accepted it) or ``injected`` (carrying
    a fabricated summary)."""
    bucket: int
    verdict: str | None = None
    reasons: tuple[str, ...] = ()
    error: str | None = None
    resolution: Resolution | None = None
    note: str = ""

    @property
    def challenged(self) -> bool:
        """Whether the event produced a challenge."""
        return self.resolution is not None

    @property
    def status(self) -> str:
        """The challenge status, or ``none``."""
        return "none" if self.resolution is None else self.resolution.status

    def as_line(self) -> str:
        """One greppable line describing the event."""
        head = (
            f"block {self.height} proof {self.proof_id[:10]} "
            f"({self.origin}, bucket {self.bucket}): "
        )
        if self.error is not None:
            return head + f"analysis failed: {self.error}"
        text = head + f"verdict {self.verdict}"
        if self.reasons:
            text += " (" + "; ".join(self.reasons) + ")"
        if self.resolution is not None:
            bond = "returned" if self.resolution.returned else "burned"
            text += (
                f"; challenge {self.resolution.status} on {len(self.resolution.panel)} "
                f"vote(s), {self.resolution.bond}-unit bond {bond}"
            )
        if self.note:
            text += f"; {self.note}"
        return text


@dataclass(frozen=True, slots=True)
class AuditReport:
    """What one audit saw, for the log line and for tests."""

    events: tuple[QualityEvent, ...] = ()
    challenged: int = 0
    upheld: int = 0
    rejected: int = 0
    blocked: str = ""
    """Why nothing was audited, when nothing was."""

    @property
    def analysed(self) -> int:
        """How many proofs were re-run."""
        return len(self.events)

    def as_line(self) -> str:
        """One greppable line per audit."""
        if not self.events:
            return f"quality: {self.blocked or 'nothing sampled'}"
        return "quality: " + "; ".join(event.as_line() for event in self.events)


# -- the auditor --------------------------------------------------------------


EngineFactory = Callable[[], AnalysisEngine]
"""``() -> engine``, so tests never start Stockfish."""


@dataclass
class QualityAudit:
    """Re-run the pinned profile over new proofs and replay one challenge.

    One instance follows the chain forward: every call to :meth:`audit` looks at
    blocks it has not seen, so a cycle with no new block costs one
    ``GET /v1/status``. Proofs older than ``challenge_window`` are skipped when a
    cycle falls behind, because a challenge against them would be refused.
    """

    client: httpx.Client
    params: NetworkParams = TESTNET
    profile: QualityProfile = QUALITY_V1
    sample_rate: int = QUALITY_SAMPLE_RATE
    max_events: int = MAX_AUDIT_EVENTS
    inject_every: int = INJECT_EVERY
    bond: int | None = None
    challenger: Challenger | None = None
    engine_factory: EngineFactory | None = None
    engine_path: Path = ENGINE_PATH
    engine_profile: str = ENGINE_PROFILE
    _engine: AnalysisEngine | None = field(default=None, init=False, repr=False)
    _owned: bool = field(default=False, init=False, repr=False)
    _cache: AnalysisCache = field(default_factory=AnalysisCache, init=False, repr=False)
    _next_height: int = field(default=1, init=False, repr=False)
    _events: int = field(default=0, init=False, repr=False)
    _epochs: dict[int, EpochRecord] = field(default_factory=dict, init=False, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.max_events < 1:
            raise QualityEventError("max_events must be at least one")
        if self.inject_every < 1:
            raise QualityEventError("inject_every must be at least one")
        if not 0 < self.sample_rate <= SAMPLE_BUCKETS:
            raise QualityEventError("sample_rate must be between 1 and 1000")
        if self.bond is None:
            self.bond = max(1, self.params.min_bond)
        if self.bond < 1:
            raise QualityEventError("the challenge bond must be positive")
        if self.challenger is None:
            self.challenger = challenger_address(self.params)

    def audit(self) -> AuditReport:
        """Re-run the newest proofs and adjudicate the challenges they warrant.

        Raises :class:`QualityEventError` when the node cannot be read; the caller
        logs that per cycle, like any other traffic failure.
        """
        if self._closed:
            raise QualityEventError("this auditor is closed")
        window = self.params.challenge_window
        if window <= 0:
            return AuditReport(blocked="this network disables quality challenges")
        height = tip_height(self.client)
        if height < self._next_height:
            return AuditReport(blocked=f"no block after {height}")
        start = max(1, self._next_height, height - window + 1)
        sampler = QualitySampler(
            self.params,
            profile=self.profile,
            sample_rate=self.sample_rate,
            cache=self._cache,
        )
        events: list[QualityEvent] = []
        for block_height in range(start, height + 1):
            if len(events) >= self.max_events:
                break
            events.extend(self._audit_block(sampler, block_height))
        self._next_height = height + 1
        return AuditReport(
            events=tuple(events),
            challenged=sum(1 for event in events if event.challenged),
            upheld=sum(1 for event in events if event.status == "upheld"),
            rejected=sum(1 for event in events if event.status == "rejected"),
            blocked="" if events else f"no new proof between blocks {start} and {height}",
        )

    def close(self) -> None:
        """Stop the engine this auditor opened, if it opened one."""
        if self._closed:
            return
        self._closed = True
        engine, self._engine = self._engine, None
        if self._owned and engine is not None:
            close = getattr(engine, "close", None)
            if callable(close):
                close()

    # -- one block ----------------------------------------------------------

    def _audit_block(self, sampler: QualitySampler, height: int) -> list[QualityEvent]:
        """Analyse the sampled proofs of ``height``, up to the event budget."""
        view = _get_object(self.client, f"/v1/blocks/{height}")
        block_hash = _digest(view.get("hash"), "hash", f"block {height}")
        miner = block_miner(view, height)
        events: list[QualityEvent] = []
        for proof in block_proofs(view, height):
            if len(events) >= self.max_events:
                break
            decision = sampling_decision(block_hash, proof.proof_id, sample_rate=self.sample_rate)
            if not decision.sampled:
                continue
            origin = self._origin()
            candidate = lying_proof(proof, self.profile) if origin == "injected" else proof
            if sampler.enqueue(block_hash, candidate) is None:
                continue
            events.append(
                self._analyse(
                    sampler,
                    proof=proof,
                    origin=origin,
                    decision=decision,
                    height=height,
                    miner=miner,
                )
            )
            self._events += 1
        return events

    def _run(self, sampler: QualitySampler) -> tuple[SamplerResult, ...]:
        """Run the sampler over the queued proof, through its retries.

        ``QualitySampler.run`` re-queues a failed job rather than returning it, so
        a single call can come back empty while the sampler still owes us a
        verdict. Asking again until it reports -- it gives up after
        ``max_attempts`` -- surfaces the engine error instead of a bare "no
        result" on the cycle line.
        """
        for _ in range(max(1, sampler.max_attempts)):
            results = sampler.run(self._analysis_engine(), limit=1)
            if results:
                return results
        return ()

    def _origin(self) -> str:
        """``injected`` on every ``inject_every``-th event, ``sampled`` otherwise."""
        last = self.inject_every - 1
        return "injected" if self._events % self.inject_every == last else "sampled"

    def _analyse(
        self,
        sampler: QualitySampler,
        *,
        proof: WinProof,
        origin: str,
        decision: SamplingDecision,
        height: int,
        miner: str,
    ) -> QualityEvent:
        """Run the sampler over the proof it just queued and build its event."""
        try:
            results = self._run(sampler)
        except (EngineError, QualityError) as exc:
            return self._event(
                proof=proof,
                origin=origin,
                decision=decision,
                height=height,
                miner=miner,
                error=str(exc),
            )
        result: SamplerResult | None = results[0] if results else None
        if result is None:
            return self._event(
                proof=proof,
                origin=origin,
                decision=decision,
                height=height,
                miner=miner,
                error="the sampler produced no result",
            )
        return self._event(
            proof=proof,
            origin=origin,
            decision=decision,
            height=height,
            miner=miner,
            result=result,
        )

    def _event(
        self,
        *,
        proof: WinProof,
        origin: str,
        decision: SamplingDecision,
        height: int,
        miner: str,
        result: SamplerResult | None = None,
        error: str | None = None,
    ) -> QualityEvent:
        """Build one event, replaying the challenge when the re-run disagrees."""
        resolution: Resolution | None = None
        note = ""
        if error is None and result is not None and result.error is not None:
            # An engine that failed to analyse is not a disagreement: surface the
            # failure instead of a bare "verdict None" on the cycle line.
            error = result.error
        if result is not None and result.needs_challenge:
            try:
                resolution = self._simulate(proof, result=result, height=height, miner=miner)
            except (ChallengeError, QualityError, QualityEventError) as exc:
                note = str(exc)
        return QualityEvent(
            height=height,
            proof_id=proof.proof_id,
            game_hash=proof.game_hash,
            miner=miner,
            origin=origin,
            bucket=decision.bucket,
            verdict=None if result is None else result.verdict,
            reasons=() if result is None else result.reasons,
            error=error,
            resolution=resolution,
            note=note,
        )

    # -- the challenge mirror -----------------------------------------------

    def _simulate(
        self,
        proof: WinProof,
        *,
        result: SamplerResult,
        height: int,
        miner: str,
    ) -> Resolution:
        """Open, seat and adjudicate the challenge in a local state.

        The mirror holds exactly what the rules need: the proof, the challenger's
        bond and the bonded accounts the panel is drawn from. Every panel member
        votes the re-run's own verdict, which is what an honest panel finds.
        """
        request = result.challenge()
        if request is None:  # pragma: no cover - the caller checks needs_challenge
            raise QualityEventError("the sampler produced no challenge request")
        assert self.bond is not None
        assert self.challenger is not None
        state = self._mirror(height=height)
        state.proofs[proof.game_hash] = ProofRecord(
            proof_id=proof.proof_id,
            game_hash=proof.game_hash,
            height=height,
            winner=proof.winner,
            miner=miner,
        )
        state.proof_ids[proof.proof_id] = proof.game_hash
        state.ensure(self.challenger).balance = self.bond
        record = challenges.open_challenge(
            state,
            challenger=self.challenger,
            proof_id=request.proof_id,
            reason=request.reason,
            evidence=request.evidence,
            bond=self.bond,
            height=height,
        )
        bad = result.verdict == "fail"
        refusals: list[str] = []
        for size in _PANEL_SIZES:
            try:
                panel = challenges.challenge_panel(state, record.challenge_id, size=size)
            except ChallengeError as exc:
                refusals.append(str(exc))
                continue
            resolved = challenges.adjudicate_with_panel(
                state,
                record.challenge_id,
                dict.fromkeys(panel, bad),
                height=height + 1,
                size=size,
            )
            return Resolution(
                challenge_id=resolved.challenge_id,
                status=resolved.status,
                panel=resolved.panel,
                bond=self.bond,
                returned=self.bond if resolved.upheld else 0,
                burned=0 if resolved.upheld else self.bond,
                slashed=resolved.upheld,
            )
        raise QualityEventError(
            "no bonded miner could be seated on a panel; the challenge would stay open "
            f"({' / '.join(refusals)})"
        )

    def _mirror(self, *, height: int) -> State:
        """A state carrying the tip epoch's beacon and the bonded roster."""
        state = State(self.params, height=height)
        epoch = self._epoch(state.current_epoch())
        if epoch is None:
            return state
        state.epochs[epoch.epoch] = epoch
        for address in epoch.roster:
            account = state.ensure(address)
            # A panel is drawn from accounts with a positive bond, and a devnet's
            # ``min_bond`` is zero, so the floor is one unit either way.
            account.bonded = max(account.bonded, self.params.min_bond, 1)
        return state

    def _epoch(self, epoch: int) -> EpochRecord | None:
        """The node's epoch record, cached; ``None`` when the chain has none yet."""
        cached = self._epochs.get(epoch)
        if cached is not None:
            return cached
        document = _get_optional(self.client, f"/v1/epochs/{epoch}")
        if document is None:
            return None
        roster: dict[str, RosterMember] = {}
        raw_roster = document.get("roster")
        if isinstance(raw_roster, Mapping):
            for address, member in raw_roster.items():
                if not isinstance(address, str) or not isinstance(member, Mapping):
                    continue
                try:
                    roster[address] = RosterMember.model_validate(member)
                except ValueError:
                    continue
        beacon_raw = document.get("beacon")
        record = EpochRecord(
            epoch=epoch,
            beacon=Beacon.model_validate(beacon_raw) if isinstance(beacon_raw, Mapping) else None,
            roster=roster,
            roster_root=str(document.get("roster_root") or ""),
        )
        self._epochs[epoch] = record
        return record

    # -- the engine ---------------------------------------------------------

    def _analysis_engine(self) -> AnalysisEngine:
        """The engine the audit re-runs with, opened on first use."""
        if self._engine is not None:
            return self._engine
        if self.engine_factory is not None:
            self._engine = self.engine_factory()
            return self._engine
        if not self.engine_path.is_file():
            raise QualityEventError(
                f"no engine binary at {self.engine_path}; the re-run needs the same "
                "Stockfish the miners play with"
            )
        self._engine = open_analysis_engine(self.engine_path, self.engine_profile)
        self._owned = True
        return self._engine


if __name__ == "__main__":  # pragma: no cover - the generator is the entry point
    raise SystemExit(
        "synthetic_quality_events is a library: run deploy/synthetic_testnet_traffic.py, "
        "which drives it (on by default), or import QualityAudit from a script."
    )
