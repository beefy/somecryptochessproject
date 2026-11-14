"""Deterministic quality events: audit a proof, accuse it, watch the chain decide.

The bonded challenge path needs three things a quiet testnet never produces on its
own: a proof that carries a quality summary, a party willing to spend a bond
accusing it, and a panel that re-runs the pinned profile. This module supplies the
second and drives the rest:

* it reads the newest blocks from ``GET /v1/blocks/{height}`` and keeps the proofs
  that carry a ``quality_summary`` -- a miner running with ``--quality-summary
  honest`` or ``forged`` (the liar in ``synthetic_miner_creation``);
* it runs the node's own :class:`~sccp.node.quality_sampler.QualitySampler` over
  them, so the sampling bucket, the PGN re-validation and the summary comparison
  are the node's code rather than a copy of it;
* when the re-run disagrees with the summary a proof claims, it signs a real
  ``challenge`` transaction from a published, faucet-funded account and submits it
  to ``POST /v1/tx``. The chain locks the bond, the bonded panel re-runs the proof
  itself and votes in ``panel_vote`` transactions, and every node replays the same
  verdict;
* every cycle it polls ``GET /v1/challenges?include_resolved=true`` and reports the
  challenges this account filed that the chain has since decided -- ``upheld`` (the
  lie was caught, the miner's bond is slashed and the challenger is repaid) or
  ``rejected`` (the proof was honest and the challenger's bond is burned).

``false_challenge_every`` files a knowingly wrong accusation against an honest
summary so the ``rejected`` branch is exercised too: an accuser who is wrong pays
for it, which is the half of the incentive that keeps challenges honest.

Nothing here is simulated. ``submit=False`` gives a read-only audit for tests and
dry runs: it reports what it would accuse and never spends a coin.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

import httpx

from sccp.chain.tx import sign_transaction
from sccp.chess.engines import open_engine
from sccp.chess.quality import (
    QUALITY_V1,
    AnalysisCache,
    AnalysisEngine,
    QualityProfile,
    summary_matches,
)
from sccp.config import TESTNET, NetworkParams
from sccp.crypto.address import encode_address
from sccp.crypto.pq import HybridKeypair, hybrid_keypair_from_seed
from sccp.errors import EngineError, QualityError
from sccp.node.quality_sampler import (
    SAMPLE_BUCKETS,
    QualitySampler,
    SamplerResult,
    SamplingDecision,
    sampling_decision,
)
from sccp.protocol import challenges
from sccp.protocol.models import Transaction, TxType, WinProof

QUALITY_SAMPLE_RATE: Final = SAMPLE_BUCKETS
"""How often the auditor looks at a summarised proof.

The node samples at ``quality_sample_rate``, which is ``0`` on testnet because
quality enforcement is off there. The auditor is not a validator: it looks at
every proof that carries a summary and lets ``max_events`` do the rate limiting.
"""

MAX_AUDIT_EVENTS: Final = 1
"""How many proofs one audit may analyse.

A depth-14 multi-PV re-run of a 60-ply game costs about what a match costs, so
one per cycle keeps the generator background load rather than the main load.
"""

CHALLENGER_NAMESPACE: Final = b"sccp/testnet/quality-challenger/"
"""The published seed of the account that files the challenges."""

ENGINE_PROFILE: Final = "stockfish-19"
"""The engine profile the miners register, so the re-run fingerprints match."""

ENGINE_PATH: Final = Path("/usr/games/stockfish")
"""The binary the stack installs; the miners play with this same file."""

FALSE_CHALLENGE_REASON: Final = "deliberate false accusation: exercising the rebuttal path"
"""What a knowingly false challenge says about itself."""


class QualityEventError(RuntimeError):
    """An audit could not read the node, fund itself or submit a challenge."""


def challenger_keypair() -> HybridKeypair:
    """The published key of the challenger account, derived from a fixed seed."""
    return hybrid_keypair_from_seed(hashlib.sha256(CHALLENGER_NAMESPACE).digest())


def challenger_address(params: NetworkParams = TESTNET) -> str:
    """The deterministic account that files the challenges."""
    return encode_address(challenger_keypair().public_key_bytes, params)


def open_analysis_engine(
    path: str | Path = ENGINE_PATH,
    profile: str = ENGINE_PROFILE,
) -> AnalysisEngine:
    """Open the engine ``sccp miner run`` uses, so the fingerprints line up."""
    return open_engine(profile, binary=Path(path))


def challenge_transaction(
    *,
    keypair: HybridKeypair,
    challenger: str,
    params: NetworkParams,
    proof_id: str,
    reason: str,
    evidence: str,
    bond: int,
    nonce: int,
) -> Transaction:
    """The signed ``challenge`` transaction an audit files.

    Kept apart from the HTTP call so the envelope can be checked against the real
    consensus rules in a test without a node: the payload keys, the bond and the
    first-use public key are exactly what ``sccp.chain.tx`` validates.
    """
    tx = Transaction(
        network=params.network_id,
        type=TxType.CHALLENGE,
        sender=challenger,
        nonce=nonce,
        fee=params.min_fee,
        payload={
            "proof_id": proof_id,
            "reason": reason,
            "evidence": evidence,
            "bond": bond,
            "public_key": keypair.public_key_hex,
        },
    )
    return sign_transaction(keypair, tx)


# -- talking to the node ------------------------------------------------------


def _get_object(client: httpx.Client, path: str) -> Mapping[str, object]:
    """GET ``path`` and return a JSON object, or raise :class:`QualityEventError`."""
    response = client.get(path)
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
class QualityEvent:
    """One audited proof and the challenge it did or did not justify."""

    height: int
    proof_id: str
    game_hash: str
    miner: str
    bucket: int
    verdict: str | None = None
    """The re-run's verdict, whether or not it matched the summary."""
    reasons: tuple[str, ...] = ()
    """Why the re-run disagreed with the summary, empty when it agreed."""
    error: str | None = None
    challenge_id: str | None = None
    """The challenge this audit filed, once the chain has it."""
    submitted: bool = False
    note: str = ""

    @property
    def challenged(self) -> bool:
        """Whether this event filed a challenge."""
        return self.submitted or self.challenge_id is not None

    def as_line(self) -> str:
        """One greppable line describing the event."""
        head = f"block {self.height} proof {self.proof_id[:10]} (bucket {self.bucket}): "
        if self.error is not None:
            return head + f"analysis failed: {self.error}"
        text = head + f"verdict {self.verdict}"
        if self.reasons:
            text += " (" + "; ".join(self.reasons) + ")"
        if self.submitted:
            text += "; challenge submitted, wait for the panel"
        elif self.challenge_id is not None:
            text += f"; challenge {self.challenge_id[:10]}"
        if self.note:
            text += f"; {self.note}"
        return text


@dataclass(frozen=True, slots=True)
class Settlement:
    """A challenge this audit filed that the chain has since decided."""

    challenge_id: str
    proof_id: str
    status: str
    """``upheld`` (the summary was false) or ``rejected`` (it was honest)."""
    bond: int
    slashed: bool

    @property
    def survived(self) -> bool:
        """Whether the proof the challenge named is still valid."""
        return self.status != "upheld"

    def as_line(self) -> str:
        """One greppable line describing the verdict."""
        outcome = (
            f"upheld on proof {self.proof_id[:10]}: miner slashed, {self.bond}-unit bond returned"
            if self.slashed
            else f"rejected on proof {self.proof_id[:10]}: proof restored and "
            f"{self.bond}-unit bond burned"
        )
        return f"challenge {self.challenge_id[:10]} {outcome}"


@dataclass(frozen=True, slots=True)
class AuditReport:
    """What one audit saw, for the log line and for tests."""

    events: tuple[QualityEvent, ...] = ()
    settlements: tuple[Settlement, ...] = ()
    stranded: tuple[str, ...] = ()
    """Filed challenges with no bonded adjudicator: their bond stays locked."""
    blocked: str = ""
    """Why nothing was audited, when nothing was."""

    @property
    def analysed(self) -> int:
        """How many proofs were re-run."""
        return len(self.events)

    @property
    def challenged(self) -> int:
        """How many challenges this audit filed."""
        return sum(1 for event in self.events if event.challenged)

    @property
    def upheld(self) -> int:
        """How many of the filed challenges the chain upheld."""
        return sum(1 for settlement in self.settlements if settlement.status == "upheld")

    @property
    def rejected(self) -> int:
        """How many of the filed challenges the chain rejected."""
        return sum(1 for settlement in self.settlements if settlement.status == "rejected")

    def as_line(self) -> str:
        """One greppable line per audit."""
        parts = [event.as_line() for event in self.events]
        parts.extend(settlement.as_line() for settlement in self.settlements)
        parts.extend(
            f"challenge {challenge_id[:10]} has no bonded adjudicator yet; its bond stays locked"
            for challenge_id in self.stranded
        )
        if not parts:
            return f"quality: {self.blocked or 'nothing sampled'}"
        return "quality: " + "; ".join(parts)


# -- the auditor --------------------------------------------------------------


EngineFactory = Callable[[], AnalysisEngine]
"""``() -> engine``, so tests never start Stockfish."""


@dataclass
class QualityAudit:
    """Re-run the pinned profile over new proofs and file bonded challenges.

    One instance follows the chain forward: every call to :meth:`audit` looks at
    blocks it has not seen, so a cycle with no new block costs one
    ``GET /v1/status``. Proofs older than ``challenge_window`` are skipped when a
    cycle falls behind, because a challenge against them would be refused.

    ``submit`` is on by default: an audit that finds a lying summary files a real
    ``challenge`` transaction and the chain's panel decides it. ``submit=False``
    is the read-only audit: the finding is reported and no bond is spent.
    """

    client: httpx.Client
    params: NetworkParams = TESTNET
    profile: QualityProfile = QUALITY_V1
    sample_rate: int = QUALITY_SAMPLE_RATE
    max_events: int = MAX_AUDIT_EVENTS
    bond: int | None = None
    challenger: str | None = None
    submit: bool = True
    false_challenge_every: int = 0
    """File a knowing false accusation every Nth analysed proof; ``0`` is never."""
    engine_factory: EngineFactory | None = None
    engine_path: Path = ENGINE_PATH
    engine_profile: str = ENGINE_PROFILE
    _keypair: HybridKeypair | None = field(default=None, init=False, repr=False)
    _engine: AnalysisEngine | None = field(default=None, init=False, repr=False)
    _owned: bool = field(default=False, init=False, repr=False)
    _cache: AnalysisCache = field(default_factory=AnalysisCache, init=False, repr=False)
    _next_height: int = field(default=1, init=False, repr=False)
    _analysed: int = field(default=0, init=False, repr=False)
    _nonce: int | None = field(default=None, init=False, repr=False)
    _settled: set[str] = field(default_factory=set, init=False, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.max_events < 1:
            raise QualityEventError("max_events must be at least one")
        if self.false_challenge_every < 0:
            raise QualityEventError("false_challenge_every cannot be negative")
        if not 0 < self.sample_rate <= SAMPLE_BUCKETS:
            raise QualityEventError("sample_rate must be between 1 and 1000")
        if self.bond is None:
            self.bond = max(1, self.params.min_bond)
        if self.bond < 1:
            raise QualityEventError("the challenge bond must be positive")
        if self.submit:
            self._keypair = challenger_keypair()
            derived = encode_address(self._keypair.public_key_bytes, self.params)
            if self.challenger is None:
                self.challenger = derived
            elif self.challenger != derived:
                raise QualityEventError(
                    f"the challenger must be {derived}; its key is derived from the "
                    "published seed and no other account can sign for it"
                )
        elif self.challenger is None:
            self.challenger = challenger_address(self.params)

    def audit(self) -> AuditReport:
        """Audit the newest summarised proofs and file the challenges they warrant.

        Raises :class:`QualityEventError` when the node cannot be read; the caller
        logs that per cycle, like any other traffic failure.
        """
        if self._closed:
            raise QualityEventError("this auditor is closed")
        window = self.params.challenge_window
        if window <= 0:
            return AuditReport(blocked="this network disables quality challenges")
        settlements, stranded = self._poll_settlements()
        height = tip_height(self.client)
        if height < self._next_height:
            return AuditReport(
                settlements=settlements,
                stranded=stranded,
                blocked=f"no block after {height}",
            )
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
        blocked = (
            ""
            if events or settlements or stranded
            else f"no proof carrying a summary between blocks {start} and {height}"
        )
        return AuditReport(
            events=tuple(events),
            settlements=settlements,
            stranded=stranded,
            blocked=blocked,
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
        """Analyse the summarised proofs of ``height``, up to the event budget."""
        view = _get_object(self.client, f"/v1/blocks/{height}")
        block_hash = _digest(view.get("hash"), "hash", f"block {height}")
        miner = block_miner(view, height)
        events: list[QualityEvent] = []
        for proof in block_proofs(view, height):
            if len(events) >= self.max_events:
                break
            if proof.quality_summary is None:
                # Only a summary can be wrong. A proof without one is what every
                # honest miner submits while testnet's quality mode is off, and
                # accusing it would slash a miner who claimed nothing.
                continue
            decision = sampling_decision(block_hash, proof.proof_id, sample_rate=self.sample_rate)
            if not decision.sampled:
                continue
            if sampler.enqueue(block_hash, proof) is None:  # pragma: no cover - dup
                continue
            self._analysed += 1
            events.append(
                self._analyse(
                    sampler,
                    proof=proof,
                    decision=decision,
                    height=height,
                    miner=miner,
                )
            )
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

    def _analyse(
        self,
        sampler: QualitySampler,
        *,
        proof: WinProof,
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
                decision=decision,
                height=height,
                miner=miner,
                error=str(exc),
            )
        result: SamplerResult | None = results[0] if results else None
        if result is None:
            return self._event(
                proof=proof,
                decision=decision,
                height=height,
                miner=miner,
                error="the sampler produced no result",
            )
        return self._event(
            proof=proof,
            decision=decision,
            height=height,
            miner=miner,
            result=result,
        )

    def _event(
        self,
        *,
        proof: WinProof,
        decision: SamplingDecision,
        height: int,
        miner: str,
        result: SamplerResult | None = None,
        error: str | None = None,
    ) -> QualityEvent:
        """Build one event, filing a challenge when the summary is false."""
        note = ""
        submitted = False
        if error is None and result is not None and result.error is not None:
            # An engine that failed to analyse is not a disagreement: surface the
            # failure instead of a bare "verdict None" on the cycle line.
            error = result.error
        reasons = () if result is None else result.reasons
        if error is None and result is not None and result.report is not None:
            agrees, finding = summary_matches(result.summary, result.report)
            deliberate = (
                self.false_challenge_every > 0 and self._analysed % self.false_challenge_every == 0
            )
            if agrees and not deliberate:
                note = f"summary agrees ({finding}); no challenge"
            elif not self.submit:
                note = "read-only audit: a challenge would be filed here"
            else:
                try:
                    self._file(proof, result, agrees=agrees, finding=finding)
                except (QualityError, QualityEventError, httpx.HTTPError) as exc:
                    note = str(exc)
                else:
                    submitted = True
        return QualityEvent(
            height=height,
            proof_id=proof.proof_id,
            game_hash=proof.game_hash,
            miner=miner,
            bucket=decision.bucket,
            verdict=None if result is None else result.verdict,
            reasons=reasons,
            error=error,
            submitted=submitted,
            note=note,
        )

    # -- filing the challenge -----------------------------------------------

    def _file(
        self,
        proof: WinProof,
        result: SamplerResult,
        *,
        agrees: bool,
        finding: str,
    ) -> None:
        """Sign and submit a ``challenge`` transaction for ``proof``."""
        evidence = json.dumps(
            {
                "kind": "quality-audit",
                "policy": self.profile.policy,
                "claimed": (
                    None if result.summary is None else result.summary.model_dump(mode="json")
                ),
                "observed_verdict": result.verdict,
                "observed_trace": None if result.report is None else result.report.trace_hash,
                "finding": finding,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        reason = (
            f"quality summary does not match an independent re-run: {finding}"
            if not agrees
            else FALSE_CHALLENGE_REASON
        )
        self._submit(
            proof,
            reason=reason[: challenges.MAX_REASON_LENGTH],
            evidence=evidence[: challenges.MAX_EVIDENCE_LENGTH],
        )

    def _submit(self, proof: WinProof, *, reason: str, evidence: str) -> None:
        """Announce the signed challenge, or explain why it cannot be afforded."""
        if self.bond is None or self._keypair is None or self.challenger is None:
            raise QualityEventError("this auditor does not submit challenges")
        balance = self._balance()
        cost = self.bond + self.params.min_fee
        if balance < cost:
            raise QualityEventError(
                f"{self.challenger} holds {balance} units; a challenge costs {cost} "
                "(bond plus fee), so the cycle's top-up has not landed yet"
            )
        nonce = self._next_nonce()
        tx = challenge_transaction(
            keypair=self._keypair,
            challenger=self.challenger,
            params=self.params,
            proof_id=proof.proof_id,
            reason=reason,
            evidence=evidence,
            bond=self.bond,
            nonce=nonce,
        )
        response = self.client.post(
            "/v1/tx",
            json=tx.model_dump(mode="json", by_alias=True),
        )
        if response.status_code >= 300:
            # The nonce may have clashed with something that landed between our
            # read and our submission; re-read it next cycle instead of drifting.
            self._nonce = None
            raise QualityEventError(
                f"POST /v1/tx refused the challenge: {response.status_code} {response.text[:200]}"
            )
        self._nonce = nonce + 1

    def _account(self) -> Mapping[str, object]:
        """The challenger's account view."""
        if self.challenger is None:  # pragma: no cover - guarded by __post_init__
            raise QualityEventError("this auditor has no challenger account")
        return _get_object(self.client, f"/v1/accounts/{self.challenger}")

    def _balance(self) -> int:
        """The challenger's spendable balance, in base units."""
        balance = self._account().get("balance")
        if not isinstance(balance, int) or isinstance(balance, bool):
            raise QualityEventError(f"/v1/accounts/{self.challenger} carries no balance")
        return balance

    def _next_nonce(self) -> int:
        """The nonce to sign the next challenge with."""
        committed = self._account().get("nonce")
        if not isinstance(committed, int) or isinstance(committed, bool):
            raise QualityEventError(f"/v1/accounts/{self.challenger} carries no nonce")
        self._nonce = committed if self._nonce is None else max(committed, self._nonce)
        return self._nonce

    # -- what the chain did with them ---------------------------------------

    def _poll_settlements(self) -> tuple[tuple[Settlement, ...], tuple[str, ...]]:
        """Challenges this account filed that the chain has, or cannot, decide."""
        if not self.submit:
            return (), ()
        try:
            document = _get_object(self.client, "/v1/challenges?include_resolved=true")
        except (QualityEventError, httpx.HTTPError):
            return (), ()  # a chain with no challenges yet is not an audit failure
        listed = document.get("challenges")
        settlements: list[Settlement] = []
        stranded: list[str] = []
        for entry in listed if isinstance(listed, list) else ():
            if not isinstance(entry, Mapping):
                continue
            if entry.get("challenger") != self.challenger:
                continue
            challenge_id = str(entry.get("challenge_id") or "")
            if not challenge_id or challenge_id in self._settled:
                continue
            status = entry.get("status")
            if status == "open":
                if not entry.get("adjudicators"):
                    # Nobody bonded is eligible, so no ballot can ever complete:
                    # say so instead of reporting a case that looks in progress.
                    stranded.append(challenge_id)
                continue
            if status not in {"upheld", "rejected"}:
                continue
            self._settled.add(challenge_id)
            bond = entry.get("bond")
            settlements.append(
                Settlement(
                    challenge_id=challenge_id,
                    proof_id=str(entry.get("proof_id") or ""),
                    status=str(status),
                    bond=bond if isinstance(bond, int) and not isinstance(bond, bool) else 0,
                    slashed=status == "upheld",
                )
            )
        return tuple(settlements), tuple(stranded)

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
