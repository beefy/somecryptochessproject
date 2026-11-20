"""An independent clock witness: audit a relay, sign what it observed.

The relay signs a receipt for every move it accepts, but a relay is one process
with one key and it is the only thing that ever sees the live board. A witness is
a second process with its own key that:

* polls the relay's public match API for the receipts of every open match;
* verifies each receipt against the witness keys it trusts, and refuses to sign
  a receipt whose clock arithmetic does not follow the protocol;
* co-signs each receipt with its own Ed25519 key, and, when a match is flagged,
  builds and hybrid-signs the clock checkpoint over the evidence window;
* serves the result in the relay's own bundle shape
  (``GET /v1/matches/{id}/receipts``), so a miner can use it as a drop-in source
  of evidence when the relay has none.

Nothing here is trusted by the chain: :func:`sccp.protocol.clocks.verify_clock_evidence`
re-checks the receipt chain, the checkpoint root and the signatures against the
registry before a ``timeout_claim`` is accepted. The witness's value is that its
signature is *independent* of the relay's, so testnet quorum (1) can still settle
a claim when the relay is down, and mainnet quorum (3) has three real signers.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from threading import Lock
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException

from sccp.config import CLOCK_EVIDENCE_PLIES, NetworkParams
from sccp.crypto.address import encode_address
from sccp.crypto.pq import HybridKeypair
from sccp.errors import NotFoundError, SccpError
from sccp.miner.matchmaker import ClockBundle, MatchView
from sccp.protocol.clocks import (
    build_clock_evidence,
    clock_window,
    opposite,
    sign_receipt,
    verify_clock_evidence,
    verify_receipt_signatures,
)
from sccp.protocol.models import (
    ClockEvidence,
    Color,
    FlagReceipt,
    MoveReceipt,
    ResultValue,
    StreamCheckpoint,
)


class WitnessError(SccpError, RuntimeError):
    """The witness could not observe or sign a match safely."""


@dataclass(slots=True)
class WitnessMatch:
    """What one witness has observed and signed for one match."""

    view: MatchView
    receipts: dict[int, MoveReceipt] = field(default_factory=dict)
    flag: FlagReceipt | None = None
    checkpoint: StreamCheckpoint | None = None
    audit: str = ""
    """Why the evidence did not verify, or ``""`` when it did."""
    signed: int = 0
    """How many receipts carry this witness's signature."""
    closed: bool = False
    """Whether this match is done from the witness's point of view.

    A finished match that never flagged cannot teach this witness anything more,
    and a flagged one has its checkpoint; either way the poll stops asking the
    relay about it, so a long-running witness does not accumulate one extra
    request per finished game on every poll.
    """


def _flagged_seat(certificate_white: str, certificate_black: str, seat: str) -> Color:
    """The colour ``seat`` plays in this match."""
    if seat == certificate_white:
        return "white"
    if seat == certificate_black:
        return "black"
    raise WitnessError("the flag receipt names a seat outside the certificate")


def _result_for(
    seat: str, certificate_white: str, certificate_black: str
) -> tuple[ResultValue, Color]:
    """``(result, winner)`` for the seat that was flagged."""
    flagged = _flagged_seat(certificate_white, certificate_black, seat)
    winner = opposite(flagged)
    return ("1-0" if winner == "white" else "0-1"), winner


def _receipt_clock(receipt: MoveReceipt | FlagReceipt) -> tuple[int, int, int]:
    """The fields a witness must not see change after signing."""
    return (receipt.received_at_ms, receipt.white_ms, receipt.black_ms)


def _known_signatures(receipt: MoveReceipt | FlagReceipt, keys: Mapping[str, str]) -> Any:
    """``receipt`` carrying only the signatures this witness can verify.

    A receipt may also carry signatures from peers this witness has no key for
    (a witness started without ``--relay-witness-key``). Verifying would then
    fail on an unknown address, so the bundle's copy keeps them but our own
    evidence drops them rather than reporting a false failure.

    The return annotation is ``Any`` because pydantic's ``model_copy`` is typed
    as ``Self`` and the two receipt kinds are a union at the call site; both
    callers reinterpret the result at the exact type they passed in.
    """
    kept = tuple(signature for signature in receipt.witnesses if signature.witness in keys)
    if len(kept) == len(receipt.witnesses):
        return receipt
    return receipt.model_copy(update={"witnesses": kept})


class WitnessService:
    """Observe a relay and publish independently signed clock evidence."""

    def __init__(
        self,
        params: NetworkParams,
        keypair: HybridKeypair,
        *,
        relay_url: str,
        relay_keys: Mapping[str, str] | None = None,
        client: httpx.Client | None = None,
        timeout: float = 10.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.params = params
        self.keypair = keypair
        self.relay_url = relay_url.rstrip("/")
        self.relay_keys = dict(relay_keys or {})
        self._http = client if client is not None else httpx.Client(timeout=timeout)
        self._owns_client = client is None
        self._clock = clock
        self._lock = Lock()
        self._matches: dict[str, WitnessMatch] = {}
        self._refusals: list[str] = []
        self._polls = 0
        self._last_poll_at: float | None = None

    # -- identity -----------------------------------------------------------

    @property
    def address(self) -> str:
        """This witness's account address."""
        return encode_address(self.keypair.public_key_bytes, self.params)

    @property
    def public_key(self) -> str:
        """This witness's hybrid public key, in the registry's hex form."""
        return self.keypair.public_key_hex

    @property
    def network(self) -> str:
        """The network id this witness signs for."""
        return self.params.network_id

    def trusted_keys(self) -> dict[str, str]:
        """The keys this witness verifies: the relay's and its own."""
        keys = dict(self.relay_keys)
        keys[self.address] = self.public_key
        return keys

    # -- observing ----------------------------------------------------------

    def poll(self) -> tuple[str, ...]:
        """Fetch one round of relay state and sign anything new.

        Returns the match ids this round touched. Relay errors are recorded and
        swallowed: a witness that exits on the first 503 is not a witness.
        """
        touched: list[str] = []
        try:
            views = self._open_matches()
        except (httpx.HTTPError, ValueError, SccpError) as exc:
            self._refuse(f"cannot list matches on {self.relay_url}: {exc}")
            return ()
        for view in views:
            match_id = view.matchup.match_id
            with self._lock:
                state = self._matches.get(match_id)
                if state is None:
                    state = WitnessMatch(view=view)
                    self._matches[match_id] = state
                else:
                    state.view = view
            try:
                bundle = self._fetch_bundle(match_id)
            except (httpx.HTTPError, ValueError, SccpError) as exc:
                self._refuse(f"{match_id}: cannot read the relay's receipts: {exc}")
                continue
            if bundle is None:
                continue
            try:
                if self._absorb(state, bundle):
                    touched.append(match_id)
            except SccpError as exc:
                self._refuse(f"{match_id}: {exc}")
            with self._lock:
                finished = view.status == "finished"
                settled = state.flag is not None and state.checkpoint is not None
                state.closed = state.closed or settled or (finished and state.flag is None)
        with self._lock:
            self._polls += 1
            self._last_poll_at = self._clock()
        return tuple(touched)

    def _open_matches(self) -> list[MatchView]:
        """Every pending or active match, plus any known match that just finished."""
        response = self._http.get(
            f"{self.relay_url}/v1/matches",
            params={"status": ["pending", "active"]},
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            raise WitnessError("the relay's /v1/matches did not return a list")
        views = [MatchView.model_validate(item) for item in payload]
        known = {view.matchup.match_id for view in views}
        with self._lock:
            waiting = [
                match_id
                for match_id, state in self._matches.items()
                if match_id not in known and not state.closed
            ]
        for match_id in waiting:
            follow_up = self._http.get(f"{self.relay_url}/v1/matches/{match_id}")
            if follow_up.status_code == 404:
                continue
            follow_up.raise_for_status()
            views.append(MatchView.model_validate(follow_up.json()))
        return views

    def _fetch_bundle(self, match_id: str) -> ClockBundle | None:
        """The relay's clock bundle for ``match_id``, or ``None`` if it has none."""
        response = self._http.get(f"{self.relay_url}/v1/matches/{match_id}/receipts")
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return ClockBundle.model_validate(response.json())

    def _absorb(self, state: WitnessMatch, bundle: ClockBundle) -> bool:
        """Verify and co-sign one bundle. Returns whether anything was signed."""
        network = self.params.network_id
        signed_any = False
        for receipt in bundle.receipts:
            with self._lock:
                existing = state.receipts.get(receipt.ply)
            if existing is not None:
                if _receipt_clock(existing) != _receipt_clock(receipt):
                    self._refuse(
                        f"{bundle.match_id}: the relay changed ply {receipt.ply} after signing it"
                    )
                continue
            self._verify_foreign(receipt, network=network)
            merged = sign_receipt(
                receipt,
                self.keypair,
                network=network,
                witness=self.address,
            )
            assert isinstance(merged, MoveReceipt)
            with self._lock:
                state.receipts[merged.ply] = merged
                state.signed += 1
            signed_any = True
        if bundle.flag is None:
            return signed_any
        with self._lock:
            already = state.flag is not None
        if not already:
            self._verify_foreign(bundle.flag, network=network)
            merged_flag = sign_receipt(
                bundle.flag,
                self.keypair,
                network=network,
                witness=self.address,
            )
            assert isinstance(merged_flag, FlagReceipt)
            with self._lock:
                state.flag = merged_flag
                state.signed += 1
            signed_any = True
        self._checkpoint(state)
        return signed_any

    def _verify_foreign(self, receipt: MoveReceipt | FlagReceipt, *, network: str) -> None:
        """Verify the relay's signature when the receipt carries one.

        A relay without a witness key publishes unsigned receipts. The witness
        still signs them -- it observed the same values -- but it records that the
        relay attests to nothing, which is exactly the state of a deployment
        missing ``--witness-key``.
        """
        if not receipt.witnesses:
            self._refuse(f"{receipt.match_id}: the relay's ply {receipt.ply} is unsigned")
            return
        if not self.relay_keys:
            return
        try:
            verify_receipt_signatures(
                receipt,
                network=network,
                public_keys=self.relay_keys,
                quorum=1,
            )
        except SccpError as exc:
            raise WitnessError(str(exc)) from None

    def _checkpoint(self, state: WitnessMatch) -> None:
        """Build and audit our hybrid checkpoint once a flag is known."""
        flag = state.flag
        if flag is None:
            return
        known = self.trusted_keys()
        with self._lock:
            receipts = tuple(
                _known_signatures(state.receipts[ply], known) for ply in sorted(state.receipts)
            )
        if not receipts:
            self._refuse(f"{flag.match_id}: the flag has no move receipts to anchor")
            return
        evidence = build_clock_evidence(
            time_control=state.view.time_control,
            receipts=receipts,
            final=_known_signatures(flag, known),
            signers=((self.address, self.keypair),),
            network=self.network,
        )
        with self._lock:
            state.checkpoint = evidence.checkpoint
        state.audit = self._audit(state, evidence)

    def _audit(self, state: WitnessMatch, evidence: ClockEvidence) -> str:
        """Re-run the protocol's verifier over our own bundle.

        This is the whole point of a witness: it does not forward the relay's
        numbers, it checks them. A failure is recorded rather than raised so the
        bundle stays inspectable; ``/v1/status`` reports it.
        """
        certificate = state.view.certificate
        result, winner = _result_for(evidence.final.seat, certificate.white, certificate.black)
        try:
            verify_clock_evidence(
                evidence,
                certificate=certificate,
                san_moves=state.view.san_moves,
                result=result,
                winner_seat=winner,
                public_keys=self.trusted_keys(),
                # Our own attestation, not the network's threshold: this proves
                # the bundle we serve is protocol-valid and that we signed it. On
                # mainnet a claim still needs quorum-many such witnesses, which is
                # the miner's assembly job, not a single witness's.
                quorum=1,
                max_plies=self.params.max_plies,
            )
        except SccpError as exc:
            self._refuse(f"{certificate.match_id}: evidence does not verify: {exc}")
            return str(exc)
        return ""

    # -- serving ------------------------------------------------------------

    def bundle(self, match_id: str) -> ClockBundle:
        """This witness's signed bundle for ``match_id``."""
        with self._lock:
            state = self._matches.get(match_id)
            if state is None:
                raise NotFoundError(f"unknown match {match_id}")
            receipts = tuple(state.receipts[ply] for ply in sorted(state.receipts))
            flag = state.flag
            checkpoint = state.checkpoint
            view = state.view
        return ClockBundle(
            match_id=match_id,
            time_control=view.time_control,
            turn=view.turn,
            white_ms=view.white_ms,
            black_ms=view.black_ms,
            last_ply=len(receipts),
            started=True,
            receipts=receipts,
            flag=flag,
            checkpoint=checkpoint,
        )

    def matches(self) -> list[dict[str, object]]:
        """A compact view of every match this witness has observed."""
        with self._lock:
            return [
                {
                    "match_id": match_id,
                    "status": state.view.status,
                    "last_ply": max(state.receipts, default=-1) + 1,
                    "signed": state.signed,
                    "flagged": state.flag is not None,
                    "audited": bool(state.checkpoint) and not state.audit,
                    "audit": state.audit,
                }
                for match_id, state in sorted(self._matches.items())
            ]

    def audit(self, match_id: str) -> dict[str, object]:
        """The verification result for one match."""
        with self._lock:
            state = self._matches.get(match_id)
            if state is None:
                raise NotFoundError(f"unknown match {match_id}")
            return {
                "match_id": match_id,
                "flag": state.flag is not None,
                "checkpoint": state.checkpoint is not None,
                "verified": bool(state.checkpoint) and not state.audit,
                "reason": state.audit,
            }

    def status(self) -> dict[str, object]:
        """Everything an operator needs to see that the witness is working."""
        with self._lock:
            return {
                "network": self.network,
                "address": self.address,
                "public_key": self.public_key,
                "relay": self.relay_url,
                "quorum": self.params.clock_witness_quorum,
                "polls": self._polls,
                "last_poll_at": self._last_poll_at,
                "matches": len(self._matches),
                "flagged": sum(1 for state in self._matches.values() if state.flag is not None),
                "audited": sum(
                    1
                    for state in self._matches.values()
                    if state.checkpoint is not None and not state.audit
                ),
                "evidence_window": CLOCK_EVIDENCE_PLIES,
                "refusals": list(dict.fromkeys(self._refusals)),
            }

    def close(self) -> None:
        """Close the HTTP client this service owns."""
        if self._owns_client:
            self._http.close()

    def _refuse(self, reason: str) -> None:
        with self._lock:
            if reason not in self._refusals:
                self._refusals.append(reason)


def evidence_window(receipts: Sequence[MoveReceipt]) -> tuple[MoveReceipt, ...]:
    """The receipts a checkpoint commits to: the last ``CLOCK_EVIDENCE_PLIES``."""
    return clock_window(receipts)


def create_witness_app(service: WitnessService) -> FastAPI:
    """The witness's read-only HTTP API.

    It mirrors the relay's receipt route on purpose: every miner already knows how
    to read a clock bundle, so a witness is a drop-in evidence source.
    """
    app = FastAPI(title="sccp clock witness", version="1")

    @app.get("/v1/status")
    def status() -> dict[str, object]:
        return service.status()

    @app.get("/v1/matches")
    def matches() -> list[dict[str, object]]:
        return service.matches()

    @app.get("/v1/matches/{match_id}/receipts", response_model=ClockBundle)
    def receipts(match_id: str) -> ClockBundle:
        try:
            return service.bundle(match_id)
        except NotFoundError:
            raise HTTPException(status_code=404, detail="unknown match") from None

    @app.get("/v1/matches/{match_id}/audit")
    def audit(match_id: str) -> dict[str, object]:
        try:
            return service.audit(match_id)
        except NotFoundError:
            raise HTTPException(status_code=404, detail="unknown match") from None

    return app


__all__ = [
    "WitnessError",
    "WitnessMatch",
    "WitnessService",
    "create_witness_app",
    "evidence_window",
]
