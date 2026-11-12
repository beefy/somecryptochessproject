"""Bonded timeout claims, disputes and witness slashing (plan 5.4 and 5.7.1).

A losing seat can always stall, so a flag must be settleable without the stalling
seat's cooperation. The mechanism is a bonded claim:

1. the winner opens ``timeout_claim`` with the certificate, its last signed move,
   the witness-signed flag bundle at zero and a bond;
2. the accused may answer with ``timeout_dispute`` before the deadline by
   producing a *later* signed move of its own, which returns the bond;
3. if the window closes with no dispute, a ``WinProof`` that references the claim
   finalises it and releases the bond to the claimant.

Finalisation is deliberately not a separate transaction: it happens when the
proof is accepted, which is the only way the win can enter the chain anyway.

The chain cannot replay the whole game at claim time (the claim carries no PGN),
so this module checks everything the claim *does* commit to: both start
signatures, the winner's last signed move, a flag receipt at the next ply with a
witness quorum, a zero clock for the flagged seat and a positive clock for the
winner. The full bundle is re-verified when the proof is validated.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import NoReturn

from sccp.chain.state import State, TimeoutClaimRecord, WitnessRecord
from sccp.crypto.keys import public_key_from_hex, verify_domain
from sccp.errors import ClockError, TimeoutClaimError, TranscriptError
from sccp.protocol.clocks import (
    move_payload,
    verify_move_signature,
    verify_receipt_signatures,
)
from sccp.protocol.models import (
    ClockEvidence,
    FlagReceipt,
    MatchCertificate,
    MoveReceipt,
    SignedMove,
    TimeoutClaim,
    Transaction,
)
from sccp.protocol.transcript import verify_certificate_signatures
from sccp.serialization import Domain

CLAIM_KEYS = frozenset(
    {"match_id", "certificate", "last_move", "clock_evidence", "bond", "public_key"}
)
DISPUTE_KEYS = frozenset({"claim_id", "move", "move_signature", "clock_evidence", "public_key"})
SLASH_KEYS = frozenset({"evidence", "proof_id", "public_key"})

EVIDENCE_KEYS = frozenset({"witness", "receipts"})
"""A witness-equivocation bundle: which witness, and the two receipts it signed."""


def _fail(message: str) -> NoReturn:
    raise TimeoutClaimError(message)


def _payload_object(payload: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = payload.get(key)
    if not isinstance(value, Mapping):
        _fail(f"payload field {key!r} must be an object")
    return value


def _payload_text(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        _fail(f"payload field {key!r} must be non-empty text")
    return value


def _payload_int(payload: Mapping[str, object], key: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        _fail(f"payload field {key!r} must be an integer")
    return value


def parse_claim(tx: Transaction) -> TimeoutClaim:
    """Build the protocol :class:`TimeoutClaim` object from a transaction payload."""
    payload = tx.payload
    unknown = sorted(set(payload) - CLAIM_KEYS)
    if unknown:
        _fail(f"unknown timeout_claim payload fields: {unknown}")
    certificate = MatchCertificate.model_validate(_payload_object(payload, "certificate"))
    evidence = ClockEvidence.model_validate(_payload_object(payload, "clock_evidence"))
    last_move = SignedMove.model_validate(_payload_object(payload, "last_move"))
    return TimeoutClaim(
        network=tx.network,
        match_id=_payload_text(payload, "match_id"),
        certificate=certificate,
        last_move=last_move,
        clock_evidence=evidence,
        bond=_payload_int(payload, "bond"),
    )


def _active_witnesses(state: State, epoch: int) -> dict[str, str]:
    """The public keys of witnesses registered and unexpired at ``epoch``."""
    return {
        address: witness.public_key
        for address, witness in state.witnesses.items()
        if witness.until_epoch >= epoch and witness.bond > 0
    }


def validate_claim(state: State, tx: Transaction, height: int) -> TimeoutClaimRecord:
    """Validate a ``timeout_claim`` and return the record it would create."""
    params = state.params
    claim = parse_claim(tx)
    certificate = claim.certificate

    if certificate.network != state.network or claim.network != state.network:
        _fail("timeout claim and certificate must name this network")
    if certificate.match_id != claim.match_id:
        _fail("timeout claim does not match its certificate's match id")
    if tx.sender not in (certificate.white, certificate.black):
        _fail(f"{tx.sender} is not a seat of match {claim.match_id}")
    if claim.bond < params.min_timeout_bond:
        _fail(f"timeout claim needs a bond of at least {params.min_timeout_bond}, got {claim.bond}")

    public_keys = state.public_keys()
    try:
        verify_certificate_signatures(certificate, public_keys)
        verify_move_signature(claim.last_move, certificate=certificate, public_keys=public_keys)
    except (TranscriptError, ValueError) as exc:
        _fail(f"timeout claim evidence is invalid: {exc}")

    final = claim.clock_evidence.final
    if final.match_id != certificate.match_id:
        _fail("flag receipt does not belong to the claim's match")
    if final.seat == certificate.white:
        flagged = "white"
    elif final.seat == certificate.black:
        flagged = "black"
    else:
        _fail("flag receipt does not name a seat of this certificate")
    if flagged == claim.last_move.seat:
        _fail("a seat cannot flag itself")
    if claim.last_move.seat != tx.sender:
        _fail(f"the claim's last move must be signed by the claimant {tx.sender}")
    if final.ply != claim.last_move.ply + 1:
        _fail(
            f"flag receipt is for ply {final.ply}, expected {claim.last_move.ply + 1} "
            "right after the claimant's last move"
        )
    if claim.clock_evidence.time_control != certificate.time_control:
        _fail("clock evidence time control does not match the certificate")
    accused_ms = final.white_ms if flagged == "white" else final.black_ms
    claimant_ms = final.black_ms if flagged == "white" else final.white_ms
    if accused_ms != 0:
        _fail(f"the flagged seat still has {accused_ms} ms on its clock")
    if claimant_ms <= 0:
        _fail("the claimant's clock is also zero, so nobody flagged")

    try:
        verify_receipt_signatures(
            final,
            network=state.network,
            public_keys=_active_witnesses(state, certificate.epoch),
            quorum=params.clock_witness_quorum,
        )
    except ClockError as exc:
        _fail(f"flag receipt fails witness verification: {exc}")

    for existing in state.timeout_claims.values():
        if existing.open and existing.match_id == claim.match_id:
            _fail(f"match {claim.match_id} already has an open timeout claim")

    from sccp.chain.tx import _require_balance, txid

    _require_balance(state, tx.sender, claim.bond + tx.fee, "the timeout claim bond")

    # The flagged seat is the one that ran out of time on its turn, and it is the
    # only seat that could answer with a later move: it is the accused.
    accused = final.seat
    return TimeoutClaimRecord(
        claim_id=txid(tx),
        claimant=tx.sender,
        accused=accused,
        match_id=claim.match_id,
        bond=claim.bond,
        opened_height=height,
        deadline_height=height + params.timeout_dispute_window,
        certificate=certificate,
        clock_evidence=claim.clock_evidence,
        last_move=claim.last_move,
    )


def validate_dispute(state: State, tx: Transaction, height: int) -> TimeoutClaimRecord:
    """Validate a ``timeout_dispute`` and return the claim it cancels."""
    unknown = sorted(set(tx.payload) - DISPUTE_KEYS)
    if unknown:
        _fail(f"unknown timeout_dispute payload fields: {unknown}")
    claim_id = _payload_text(tx.payload, "claim_id")
    record = state.timeout_claims.get(claim_id)
    if record is None:
        _fail(f"no timeout claim {claim_id}")
    if not record.open:
        _fail(f"timeout claim {claim_id} is already resolved")
    if tx.sender != record.accused:
        _fail(f"only the accused seat {record.accused} may dispute claim {claim_id}")
    if height > record.deadline_height:
        _fail(
            f"the dispute window for claim {claim_id} closed at height "
            f"{record.deadline_height}; this block is {height}"
        )
    move = SignedMove.model_validate(_payload_object(tx.payload, "move"))
    signature = _payload_text(tx.payload, "move_signature")
    if move.match_id != record.match_id:
        _fail("the dispute move belongs to a different match")
    if move.seat != tx.sender:
        _fail("the dispute move must be signed by the accused seat")
    if move.ply <= record.last_move.ply:
        _fail(
            f"the dispute move is ply {move.ply}, which is not after the claimed last move "
            f"{record.last_move.ply}"
        )
    key = state.public_keys().get(tx.sender)
    if key is None:
        _fail(f"no registered public key for {tx.sender}")
    try:
        raw_signature = bytes.fromhex(signature)
    except ValueError:
        _fail("dispute move signature is not valid hex")
    if not verify_domain(public_key_from_hex(key), Domain.MOVE, move_payload(move), raw_signature):
        _fail("dispute move signature does not verify")
    return record


def apply_dispute(state: State, tx: Transaction, height: int) -> None:
    """Record a dispute and return the claimant's bond."""
    record = validate_dispute(state, tx, height)
    record.disputed_height = height
    state.ensure(record.claimant).balance += record.bond


def finalise_claim(state: State, claim_id: str, height: int) -> TimeoutClaimRecord:
    """Finalise a claim whose dispute window closed without an answer.

    Returns the claimant's bond and marks the claim finalised.
    """
    record = state.timeout_claims.get(claim_id)
    if record is None:
        _fail(f"no timeout claim {claim_id} to finalise")
    if record.disputed_height is not None:
        _fail(f"timeout claim {claim_id} was disputed and cannot finalise")
    if record.finalised_height is not None:
        return record
    if height <= record.deadline_height:
        _fail(
            f"timeout claim {claim_id} can only finalise after height "
            f"{record.deadline_height}; this block is {height}"
        )
    record.finalised_height = height
    state.ensure(record.claimant).balance += record.bond
    return record


def _receipt_from_payload(value: object) -> MoveReceipt | FlagReceipt:
    if not isinstance(value, Mapping):
        _fail("each slash-evidence receipt must be an object")
    kind = value.get("kind")
    try:
        if kind == "move":
            return MoveReceipt.model_validate(value)
        if kind == "flag":
            return FlagReceipt.model_validate(value)
    except ValueError as exc:
        _fail(f"malformed slash-evidence receipt: {exc}")
    _fail(f"unknown receipt kind {kind!r}")


def _receipt_identity(receipt: MoveReceipt | FlagReceipt) -> tuple[str, str, int]:
    return (receipt.match_id, receipt.kind, receipt.ply)


def validate_slash(state: State, tx: Transaction, height: int) -> tuple[str, WitnessRecord]:
    """Validate clock-witness equivocation evidence and return the witness."""
    unknown = sorted(set(tx.payload) - SLASH_KEYS)
    if unknown:
        _fail(f"unknown slash_evidence payload fields: {unknown}")
    if "proof_id" in tx.payload:
        _fail("proof equivocation is not an M3 rule; slash_evidence in M3 covers clock witnesses")
    evidence = _payload_object(tx.payload, "evidence")
    extra = sorted(set(evidence) - EVIDENCE_KEYS)
    if extra:
        _fail(f"unknown slash-evidence fields: {extra}")
    witness_address = _payload_text(evidence, "witness")
    receipts_raw = evidence.get("receipts")
    if not isinstance(receipts_raw, list) or len(receipts_raw) != 2:
        _fail("slash evidence needs exactly two receipts")
    first = _receipt_from_payload(receipts_raw[0])
    second = _receipt_from_payload(receipts_raw[1])
    record = state.witnesses.get(witness_address)
    if record is None:
        _fail(f"{witness_address} is not a registered clock witness")
    if record.bond <= 0:
        _fail(f"{witness_address} has no bond left to slash")
    if _receipt_identity(first) != _receipt_identity(second):
        _fail("equivocation needs two receipts for the same match, kind and ply")
    if first == second:
        _fail("the two receipts are identical, which proves nothing")
    try:
        for receipt in (first, second):
            verify_receipt_signatures(
                receipt,
                network=state.network,
                public_keys={witness_address: record.public_key},
                quorum=1,
            )
    except ClockError as exc:
        _fail(f"slash evidence fails witness verification: {exc}")
    return witness_address, record


def apply_slash(state: State, tx: Transaction, height: int) -> None:
    """Burn the equivocating witness's bond and de-register it."""
    address, record = validate_slash(state, tx, height)
    account = state.ensure(address)
    slash = min(record.bond, account.bonded)
    account.bonded -= slash
    account.banned_until = max(account.banned_until, height + state.params.unbond_delay)
    state.witnesses.pop(address, None)


__all__ = [
    "CLAIM_KEYS",
    "DISPUTE_KEYS",
    "EVIDENCE_KEYS",
    "SLASH_KEYS",
    "apply_dispute",
    "apply_slash",
    "finalise_claim",
    "parse_claim",
    "validate_claim",
    "validate_dispute",
    "validate_slash",
]
