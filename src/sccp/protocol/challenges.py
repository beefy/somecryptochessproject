"""Bonded quality challenges and adjudication.

A quality summary on a proof is evidence, not consensus. When a validator's
deterministic sample disagrees, or when any bonded party believes a proof was
fabricated, the remedy is a bonded ``challenge`` transaction. The challenge locks
the challenger's bond for ``challenge_window`` blocks; a bonded panel re-runs the
pinned quality profile and resolves it:

* **upheld** - the proof was bad. The challenger's bond is returned, the proof
  miner's bond is slashed, and the still-unvested coinbase credited at the proof's
  height is burned.
* **rejected** - the proof was good. The challenger's bond is burned.

The module never runs an engine. Adjudication receives the panel's verdict, and
the panel itself is selected from the beacon by :func:`select_panel` (or by an
operator key on testnet while panel selection is not yet deployed). That keeps
this file a deterministic state transition, which is what lets it live inside
the ledger: every node derives the same panel from the same beacon, replays the
same pinned profile, and resolves the challenge with :func:`adjudicate_with_panel`.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import NoReturn

from sccp.chain.state import ZERO_HASH, ChallengeRecord, State
from sccp.errors import ChallengeError
from sccp.protocol.models import Transaction
from sccp.serialization import Domain, domain_hex

CHALLENGE_KEYS = frozenset({"proof_id", "reason", "evidence", "bond", "public_key"})
"""Exactly the payload keys a ``challenge`` transaction may carry."""

MAX_REASON_LENGTH: int = 120
MAX_EVIDENCE_LENGTH: int = 4_096

PANEL_SIZE: int = 3
"""How many bonded miners re-run the pinned profile for one challenge.

Odd, so a majority always exists and no vote can tie. Testnet may use a smaller
operator-run panel by passing ``size=1`` explicitly.
"""


def _fail(message: str) -> NoReturn:
    raise ChallengeError(message)


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


@dataclass(frozen=True, slots=True)
class ChallengeRequest:
    """The decoded, schema-checked contents of a challenge transaction."""

    proof_id: str
    reason: str
    evidence: str
    bond: int


def parse_challenge(tx: Transaction) -> ChallengeRequest:
    """Decode a ``challenge`` payload, rejecting unknown fields."""
    unknown = sorted(set(tx.payload) - CHALLENGE_KEYS)
    if unknown:
        _fail(f"unknown challenge payload fields: {unknown}")
    proof_id = _payload_text(tx.payload, "proof_id")
    reason = _payload_text(tx.payload, "reason")
    evidence = _payload_text(tx.payload, "evidence")
    bond = _payload_int(tx.payload, "bond")
    if len(reason) > MAX_REASON_LENGTH:
        _fail(f"challenge reason is longer than {MAX_REASON_LENGTH} characters")
    if len(evidence) > MAX_EVIDENCE_LENGTH:
        _fail(f"challenge evidence is longer than {MAX_EVIDENCE_LENGTH} characters")
    if bond < 1:
        _fail("a challenge needs a positive bond")
    return ChallengeRequest(proof_id=proof_id, reason=reason, evidence=evidence, bond=bond)


def challenge_id(
    network: str,
    proof_id: str,
    challenger: str,
    reason: str,
    evidence: str,
    bond: int,
    opened_height: int,
) -> str:
    """``H("sccp/v1/challenge", request fields)`` as lowercase hex."""
    return domain_hex(
        Domain.CHALLENGE,
        {
            "network": network,
            "proof_id": proof_id,
            "challenger": challenger,
            "reason": reason,
            "evidence": evidence,
            "bond": bond,
            "opened_height": opened_height,
        },
    )


def _record_for(
    state: State,
    request: ChallengeRequest,
    challenger: str,
    height: int,
) -> ChallengeRecord:
    """Build the candidate record, without checking state or balances."""
    proof = state.proofs[state.proof_ids[request.proof_id]]
    return ChallengeRecord(
        challenge_id=challenge_id(
            state.network,
            request.proof_id,
            challenger,
            request.reason,
            request.evidence,
            request.bond,
            height,
        ),
        proof_id=request.proof_id,
        game_hash=proof.game_hash,
        challenger=challenger,
        miner=proof.miner or proof.winner,
        reason=request.reason,
        evidence=request.evidence,
        bond=request.bond,
        opened_height=height,
        deadline_height=height + state.params.challenge_window,
    )


def validate_challenge(
    state: State,
    tx: Transaction,
    height: int,
    *,
    request: ChallengeRequest | None = None,
    fee_charged: bool = False,
) -> ChallengeRecord:
    """Validate a challenge in ``state`` and return the record it would create.

    ``fee_charged`` is set by :func:`apply_challenge`: a block applies
    transactions after validation and after deducting the fee, so the apply-time
    re-check must not demand the fee twice.
    """
    decoded = request or parse_challenge(tx)
    game_hash = state.proof_ids.get(decoded.proof_id)
    if game_hash is None:
        _fail(f"no accepted proof {decoded.proof_id} to challenge")
    proof = state.proofs[game_hash]
    if tx.sender in (proof.miner, proof.winner):
        _fail("a proof's miner cannot challenge its own proof")

    params = state.params
    if params.challenge_window <= 0:
        _fail("quality challenges are disabled on this network")
    deadline = proof.height + params.challenge_window
    if height > deadline:
        _fail(
            f"the challenge window for proof {decoded.proof_id} closed at height "
            f"{deadline}; this block is {height}"
        )

    for existing in state.challenges.values():
        if existing.proof_id != decoded.proof_id:
            continue
        if existing.upheld:
            _fail(f"proof {decoded.proof_id} has already been invalidated by a challenge")
        if existing.open and existing.challenger == tx.sender:
            _fail(f"{tx.sender} already has an open challenge against this proof")

    from sccp.chain.tx import _require_balance

    required = decoded.bond if fee_charged else decoded.bond + tx.fee
    _require_balance(state, tx.sender, required, "the challenge bond")
    return _record_for(state, decoded, tx.sender, height)


def apply_challenge(state: State, tx: Transaction, height: int) -> None:
    """Lock the challenger's bond and store the challenge record."""
    record = validate_challenge(state, tx, height, fee_charged=True)
    state.ensure(tx.sender).balance -= record.bond
    state.challenges[record.challenge_id] = record


def _check_open_fields(
    state: State,
    challenger: str,
    proof_id: str,
    reason: str,
    evidence: str,
    bond: int,
    height: int,
) -> None:
    """Shared state checks for :func:`open_challenge`."""
    if len(reason) > MAX_REASON_LENGTH:
        _fail(f"challenge reason is longer than {MAX_REASON_LENGTH} characters")
    if len(evidence) > MAX_EVIDENCE_LENGTH:
        _fail(f"challenge evidence is longer than {MAX_EVIDENCE_LENGTH} characters")
    if bond < 1:
        _fail("a challenge needs a positive bond")
    game_hash = state.proof_ids.get(proof_id)
    if game_hash is None:
        _fail(f"no accepted proof {proof_id} to challenge")
    proof = state.proofs[game_hash]
    if challenger in (proof.miner, proof.winner):
        _fail("a proof's miner cannot challenge its own proof")
    params = state.params
    if params.challenge_window <= 0:
        _fail("quality challenges are disabled on this network")
    if height > proof.height + params.challenge_window:
        _fail(f"the challenge window for proof {proof_id} has closed")
    for existing in state.challenges.values():
        if existing.proof_id != proof_id:
            continue
        if existing.upheld:
            _fail(f"proof {proof_id} has already been invalidated by a challenge")
        if existing.open and existing.challenger == challenger:
            _fail(f"{challenger} already has an open challenge against this proof")


def open_challenge(
    state: State,
    *,
    challenger: str,
    proof_id: str,
    reason: str,
    evidence: str,
    bond: int,
    height: int,
) -> ChallengeRecord:
    """Open a challenge without a transaction envelope.

    This is the in-process path used by tests, devnet tooling and a node's
    automatic sampler. It applies the same checks as the transaction path and
    locks the bond, but charges no fee and consumes no nonce.
    """
    if not reason or not evidence:
        _fail("a challenge needs a reason and evidence")
    _check_open_fields(state, challenger, proof_id, reason, evidence, bond, height)
    account = state.ensure(challenger)
    if account.balance < bond:
        _fail(f"{challenger} cannot cover the challenge bond of {bond}")
    account.balance -= bond
    request = ChallengeRequest(proof_id=proof_id, reason=reason, evidence=evidence, bond=bond)
    record = _record_for(state, request, challenger, height)
    state.challenges[record.challenge_id] = record
    return record


def _burn_coinbase_for(
    state: State,
    address: str,
    proof_height: int,
    height: int,
) -> int:
    """Burn ``address``'s still-immature coinbase credited at ``proof_height``."""
    account = state.ensure(address)
    maturity = state.params.coinbase_maturity
    burned = 0
    kept: list[tuple[int, int]] = []
    for credit_height, amount in account.coinbase_queue:
        if credit_height == proof_height and height < credit_height + maturity:
            burned += amount
        else:
            kept.append((credit_height, amount))
    if burned:
        account.coinbase_queue = kept
        account.immature = max(0, account.immature - burned)
    return burned


def resolve_challenge(
    state: State,
    challenge: str,
    *,
    upheld: bool,
    height: int,
    panel: Sequence[str] = (),
) -> ChallengeRecord:
    """Resolve an open challenge from the panel's verdict.

    ``panel`` names the accounts that re-ran the profile. It is recorded in the
    challenge so the decision is auditable; the caller is responsible for having
    selected it deterministically and for having run the pinned profile.
    """
    record = state.challenges.get(challenge)
    if record is None:
        _fail(f"no challenge {challenge}")
    if not record.open:
        expected = "upheld" if upheld else "rejected"
        if record.status == expected:
            return record
        _fail(f"challenge {challenge} is already resolved as {record.status}")
    if height < record.opened_height:
        _fail("a challenge cannot resolve before the block that opened it")

    members = tuple(str(member) for member in panel)
    if len(set(members)) != len(members):
        _fail("the adjudication panel contains duplicate members")
    if record.challenger in members or record.miner in members:
        _fail("a challenge's parties cannot sit on its adjudication panel")

    record.resolved_height = height
    record.panel = members
    if upheld:
        record.status = "upheld"
        state.ensure(record.challenger).balance += record.bond
        state.ensure(record.miner).bonded = 0
        proof = state.proofs.get(record.game_hash)
        if proof is not None:
            _burn_coinbase_for(state, record.miner, proof.height, height)
    else:
        record.status = "rejected"
        # The bond was subtracted when the challenge opened; not returning it is
        # the burn. No account receives it.
    return record


adjudicate_challenge = resolve_challenge
"""Alias used by panel tooling and tests."""


def _panel_rank(beacon_hash: str, proof_id: str, member: str) -> str:
    """``H("sccp/v1/challenge", {kind: "panel", ...})`` for one candidate."""
    return domain_hex(
        Domain.CHALLENGE,
        {
            "kind": "panel",
            "beacon": beacon_hash,
            "proof_id": proof_id,
            "member": member,
        },
    )


def _check_digest(name: str, digest: str) -> None:
    try:
        raw = bytes.fromhex(digest)
    except ValueError:
        _fail(f"{name} must be hex: {digest!r}")
    if len(raw) != 32:
        _fail(f"{name} must be a 32-byte digest")


def select_panel(
    beacon_hash: str,
    proof_id: str,
    candidates: Iterable[str],
    *,
    size: int = PANEL_SIZE,
    exclude: Iterable[str] = (),
) -> tuple[str, ...]:
    """Pick the adjudication panel for one challenge, deterministically.

    Candidates are ranked by
    ``H("sccp/v1/challenge", {kind: "panel", beacon, proof_id, member})``, so every
    node that sees the same beacon and the same bonded set picks the same panel
    without coordination. ``exclude`` carries the challenge's parties, who may
    never adjudicate their own case. The result is sorted by rank, so it is also
    stable under differing candidate iteration order.
    """
    if size < 1 or size % 2 == 0:
        _fail("an adjudication panel needs an odd, positive size")
    _check_digest("beacon_hash", beacon_hash)
    _check_digest("proof_id", proof_id)
    banned = {str(member) for member in exclude}
    unique = {str(member) for member in candidates if str(member) not in banned}
    if len(unique) < size:
        _fail(f"only {len(unique)} eligible adjudicator(s) for a panel of {size}")
    ranked = sorted(unique, key=lambda member: _panel_rank(beacon_hash, proof_id, member))
    return tuple(ranked[:size])


def panel_verdict(panel: Sequence[str], votes: Mapping[str, bool]) -> bool:
    """Aggregate a panel's re-run verdicts into ``upheld`` (``True``) or not.

    Every panel member must vote and nobody else may: a missing vote would let a
    node resolve a case with a minority, and an extra vote would let a stranger
    stuff the panel. Votes must be booleans so ``"false"`` cannot read as true.
    """
    members = tuple(str(member) for member in panel)
    if not members or len(members) % 2 == 0:
        _fail("a panel verdict needs an odd number of members")
    if len(set(members)) != len(members):
        _fail("the adjudication panel contains duplicate members")
    for member, vote in votes.items():
        if not isinstance(vote, bool):
            _fail(f"panel vote from {member} must be a boolean")
    missing = sorted(set(members) - set(votes))
    unknown = sorted(set(votes) - set(members))
    if missing or unknown:
        _fail(f"panel votes do not match the panel (missing {missing}, unknown {unknown})")
    upheld = sum(1 for member in members if votes[member])
    return upheld * 2 > len(members)


def challenge_panel(
    state: State,
    challenge: str,
    *,
    size: int = PANEL_SIZE,
) -> tuple[str, ...]:
    """The deterministic panel for ``challenge``, from the current bonded set.

    The beacon is the one recorded for the tip's epoch, so the panel for a
    challenge resolved at height ``h`` is a pure function of the state at ``h``.
    Candidates are accounts with a bond; the challenge's own parties are excluded
    by :func:`select_panel`. A resolved challenge keeps the panel it was decided
    by, because re-deriving it later could name a different set.
    """
    record = state.challenges.get(challenge)
    if record is None:
        _fail(f"no challenge {challenge}")
    if record.panel:
        return record.panel
    epoch = state.epochs.get(state.current_epoch())
    beacon_hash = epoch.beacon.hash if epoch is not None and epoch.beacon else ZERO_HASH
    candidates = [address for address, account in state.accounts.items() if account.bonded > 0]
    return select_panel(
        beacon_hash,
        record.proof_id,
        candidates,
        size=size,
        exclude=(record.challenger, record.miner),
    )


def adjudicate_with_panel(
    state: State,
    challenge: str,
    votes: Mapping[str, bool],
    *,
    height: int,
    size: int = PANEL_SIZE,
) -> ChallengeRecord:
    """Resolve ``challenge`` from a majority of its panel's re-run verdicts.

    ``votes`` maps every selected panel member to whether its re-run of the pinned
    profile found the proof bad. The majority becomes the ``upheld`` flag handed
    to :func:`resolve_challenge`, which is where bonds move, so an upheld verdict
    slashes the proof miner and a rejected one burns the challenger's bond.
    """
    panel = challenge_panel(state, challenge, size=size)
    upheld = panel_verdict(panel, votes)
    return resolve_challenge(state, challenge, upheld=upheld, height=height, panel=panel)


def challenge_for_proof(
    state: State,
    proof_id: str,
    *,
    status: str | None = None,
) -> tuple[ChallengeRecord, ...]:
    """Every challenge against ``proof_id``, optionally filtered by status."""
    records = [
        record
        for record in state.challenges.values()
        if record.proof_id == proof_id and (status is None or record.status == status)
    ]
    return tuple(sorted(records, key=lambda record: (record.opened_height, record.challenge_id)))


def challenge_status(state: State, game_hash: str) -> str:
    """The most significant challenge status for ``game_hash``.

    Precedence is ``upheld`` > ``open`` > ``rejected``: an invalidation is the
    fact a user needs to see, an open case is next most important, and a rejected
    challenge does not change the proof's validity.
    """
    statuses = {
        record.status for record in state.challenges.values() if record.game_hash == game_hash
    }
    for candidate in ("upheld", "open", "rejected"):
        if candidate in statuses:
            return candidate
    return "none"


__all__ = [
    "CHALLENGE_KEYS",
    "MAX_EVIDENCE_LENGTH",
    "MAX_REASON_LENGTH",
    "PANEL_SIZE",
    "ChallengeRequest",
    "adjudicate_challenge",
    "adjudicate_with_panel",
    "apply_challenge",
    "challenge_for_proof",
    "challenge_id",
    "challenge_panel",
    "challenge_status",
    "open_challenge",
    "panel_verdict",
    "parse_challenge",
    "resolve_challenge",
    "select_panel",
    "validate_challenge",
]
