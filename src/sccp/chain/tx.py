"""Transaction validation and application (PROTOCOL.md section 10).

Validation order is fixed by the protocol: schema -> network -> signature -> nonce ->
balance -> type-specific rules -> size limits. Mempool admission runs the same
checklist; blocks rerun everything from scratch, so a mempool bug can never make
an invalid block valid.

Failures are :class:`~sccp.errors.TxValidationError` with a stable
:class:`TxFailure` reason, which is what makes a double spend, a wrong nonce, an
insufficient balance, a wrong network and a bad signature distinguishable to a
caller instead of five variations of "invalid".

Balance arithmetic uses only integers in base units. Fees are always debited from
spendable balance and never from a bond.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import NoReturn

from sccp.chain.state import State, epoch_of, first_matchable_epoch
from sccp.config import NetworkParams
from sccp.crypto.address import encode_address, validate_address
from sccp.crypto.pq import (
    HYBRID_PUBLIC_KEY_SIZE,
    HybridKeypair,
    hybrid_sign,
    hybrid_verify,
    is_hybrid_public_key_hex,
)
from sccp.errors import AddressError, KeyEncodingError, TxValidationError
from sccp.protocol.beacon import commitment
from sccp.protocol.models import Availability, Transaction, TxType
from sccp.serialization import Domain, canonical_json, domain_hex, normalize

MAX_SAFE_TEXT: int = 256
"""Upper bound for the free-text fields a payload may carry."""


class TxFailure(StrEnum):
    """Stable machine-readable reasons a transaction can fail."""

    SCHEMA = "schema"
    NETWORK = "network"
    SIGNATURE = "signature"
    NONCE = "nonce"
    BALANCE = "balance"
    FEE = "fee"
    TYPE = "type"
    SIZE = "size"
    DUPLICATE = "duplicate"
    WINDOW = "window"
    BOND = "bond"
    REGISTRATION = "registration"


def _fail(reason: TxFailure, message: str) -> NoReturn:
    raise TxValidationError(reason, message)


def tx_body(tx: Transaction) -> dict[str, object]:
    """The canonical signed body of a transaction, without its signature."""
    return {
        "network": tx.network,
        "type": tx.type.value,
        "from": tx.sender,
        "nonce": tx.nonce,
        "fee": tx.fee,
        "payload": normalize(tx.payload),
    }


def txid(tx: Transaction) -> str:
    """``H("sccp/v1/tx", body_without_signature)`` as lowercase hex."""
    return domain_hex(Domain.TX, tx_body(tx))


def tx_size(tx: Transaction) -> int:
    """Canonical byte size of the transaction, used for the size cap."""
    return len(canonical_json(tx.model_dump(mode="json", by_alias=True)))


def sign_transaction(keypair: HybridKeypair, tx: Transaction) -> Transaction:
    """Return ``tx`` with ``keypair``'s hybrid signature over its txid.

    There is exactly one signing scheme. A transaction signature is always the
    3374-byte ``scheme || ed25519 || mldsa65`` blob, so a validator never has to
    infer a scheme from a length or a key it has not authenticated yet.
    """
    if not isinstance(keypair, HybridKeypair):
        raise TxValidationError(
            TxFailure.SIGNATURE, "signing needs a HybridKeypair (the only scheme)"
        )
    unsigned = tx if tx.signature is None else tx.model_copy(update={"signature": None})
    signature = hybrid_sign(keypair, bytes.fromhex(txid(unsigned)))
    return unsigned.model_copy(update={"signature": signature.hex()})


def make_coinbase(miner: str, amount: int, network: str) -> Transaction:
    """The single coinbase transaction of a block.

    A coinbase is an unsigned ``transfer`` from the miner to itself: its
    authorisation is the block seal, not a signature, and it is never applied as
    an ordinary transaction (so it consumes no nonce).
    """
    if amount < 0:
        _fail(TxFailure.TYPE, "coinbase amount must be non-negative")
    return Transaction(
        network=network,
        type=TxType.TRANSFER,
        sender=miner,
        nonce=0,
        fee=0,
        payload={"to": miner, "amount": amount},
        signature=None,
    )


def coinbase_id(miner: str, amount: int, network: str) -> str:
    """The txid of a coinbase."""
    return txid(make_coinbase(miner, amount, network))


def resolve_public_key(state: State, tx: Transaction) -> str:
    """The sender's public key hex, from state or a first-use reveal."""
    account = state.accounts.get(tx.sender)
    if account is not None and account.public_key:
        return account.public_key
    candidate = tx.payload.get("public_key")
    if isinstance(candidate, str) and is_hybrid_public_key_hex(candidate):
        return candidate
    _fail(
        TxFailure.SIGNATURE,
        f"{tx.sender} has no registered public key and the payload carries none",
    )


def check_sender_key(state: State, params: NetworkParams, tx: Transaction) -> str:
    """Verify the sender's hybrid signature and return the public key hex it used.

    The key must be the 1985-byte hybrid blob that re-encodes to ``tx.sender``;
    a key of any other length is a schema violation rather than a second scheme,
    and a signature is always the hybrid blob. Nothing here dispatches on a
    scheme: there is only one (docs/PROTOCOL.md section 2).
    """
    key_hex = resolve_public_key(state, tx)
    try:
        raw_key = bytes.fromhex(key_hex)
    except ValueError:
        _fail(TxFailure.SIGNATURE, "public key is not valid hex")
    if len(raw_key) != HYBRID_PUBLIC_KEY_SIZE or not is_hybrid_public_key_hex(key_hex):
        _fail(
            TxFailure.SIGNATURE,
            f"public key must be a {HYBRID_PUBLIC_KEY_SIZE}-byte hybrid blob, got {len(raw_key)}",
        )
    try:
        derived = encode_address(raw_key, params)
    except (KeyEncodingError, AddressError) as exc:
        _fail(TxFailure.SIGNATURE, f"invalid hybrid public key: {exc}")
    if derived != tx.sender:
        _fail(TxFailure.SIGNATURE, f"public key derives {derived}, not the sender {tx.sender}")
    if tx.signature is None:
        _fail(TxFailure.SIGNATURE, "transaction is not signed")
    try:
        signature = bytes.fromhex(tx.signature)
    except ValueError:
        _fail(TxFailure.SIGNATURE, "signature is not valid hex")
    if not hybrid_verify(raw_key, bytes.fromhex(txid(tx)), signature):
        _fail(TxFailure.SIGNATURE, "hybrid signature does not cover the txid")
    return key_hex


# -- payload field readers ----------------------------------------------------


def _field_int(payload: Mapping[str, object], key: str, *, minimum: int = 0) -> int:
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        _fail(TxFailure.SCHEMA, f"payload field {key!r} must be an integer")
    if value < minimum:
        _fail(TxFailure.SCHEMA, f"payload field {key!r} must be at least {minimum}")
    return value


def _field_text(payload: Mapping[str, object], key: str, *, required: bool = True) -> str | None:
    value = payload.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value or len(value) > MAX_SAFE_TEXT:
        _fail(TxFailure.SCHEMA, f"payload field {key!r} must be printable text")
    if not value.isprintable():
        _fail(TxFailure.SCHEMA, f"payload field {key!r} must be printable text")
    return value


def _field_address(payload: Mapping[str, object], key: str, params: NetworkParams) -> str:
    value = _field_text(payload, key)
    if value is None or not validate_address(value, params):
        _fail(TxFailure.SCHEMA, f"payload field {key!r} is not a {params.network_id} address")
    return value


def _field_digest(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or len(value) != 64:
        _fail(TxFailure.SCHEMA, f"payload field {key!r} must be a 32-byte lowercase hex digest")
    try:
        bytes.fromhex(value)
    except ValueError:
        _fail(TxFailure.SCHEMA, f"payload field {key!r} must be lowercase hex")
    if value != value.lower():
        _fail(TxFailure.SCHEMA, f"payload field {key!r} must be lowercase hex")
    return value


def _check_unknown_keys(payload: Mapping[str, object], allowed: frozenset[str]) -> None:
    unknown = sorted(set(payload) - allowed)
    if unknown:
        _fail(TxFailure.SCHEMA, f"unknown payload fields: {unknown}")


def _require_balance(state: State, address: str, amount: int, what: str) -> None:
    available = state.spendable(address)
    if available < amount:
        _fail(
            TxFailure.BALANCE,
            f"{address} cannot cover {what} of {amount} with a spendable balance of {available}",
        )


# -- per-type validation ------------------------------------------------------

ALLOWED: dict[TxType, frozenset[str]] = {
    TxType.TRANSFER: frozenset({"to", "amount", "public_key"}),
    TxType.REGISTER: frozenset({"public_key", "endpoint", "engine", "bond"}),
    TxType.REGISTER_WITNESS: frozenset({"public_key", "endpoint", "bond"}),
    TxType.UPDATE_REGISTRATION: frozenset(
        {"endpoint", "engine", "extend_epochs", "bond", "public_key"}
    ),
    TxType.UNREGISTER: frozenset({"public_key"}),
    TxType.WITHDRAW_BOND: frozenset({"public_key"}),
    TxType.AVAILABILITY: frozenset({"epoch", "max_games", "commitment", "public_key"}),
    TxType.REVEAL: frozenset({"epoch", "nonce", "public_key"}),
    TxType.TIMEOUT_CLAIM: frozenset(
        {"match_id", "certificate", "last_move", "clock_evidence", "bond", "public_key"}
    ),
    TxType.TIMEOUT_DISPUTE: frozenset(
        {"claim_id", "move", "move_signature", "clock_evidence", "public_key"}
    ),
    TxType.CHALLENGE: frozenset({"proof_id", "reason", "evidence", "bond", "public_key"}),
    TxType.SLASH_EVIDENCE: frozenset({"proof_id", "evidence", "public_key"}),
}
"""Exactly which payload keys each type may carry."""


def _validate_transfer(params: NetworkParams, state: State, tx: Transaction, height: int) -> None:
    _check_unknown_keys(tx.payload, ALLOWED[TxType.TRANSFER])
    amount = _field_int(tx.payload, "amount", minimum=1)
    to = _field_address(tx.payload, "to", params)
    if to == tx.sender:
        _fail(TxFailure.TYPE, "a transfer must name a different recipient")
    _require_balance(state, tx.sender, amount + tx.fee, "the transfer")


def _validate_register(params: NetworkParams, state: State, tx: Transaction, height: int) -> None:
    _check_unknown_keys(tx.payload, ALLOWED[TxType.REGISTER])
    bond = _field_int(tx.payload, "bond")
    endpoint = _field_text(tx.payload, "endpoint")
    engine = _field_text(tx.payload, "engine")
    if bond < params.min_bond:
        _fail(TxFailure.BOND, f"register needs a bond of at least {params.min_bond}, got {bond}")
    account = state.account(tx.sender)
    if height < account.banned_until:
        _fail(TxFailure.REGISTRATION, f"{tx.sender} is banned until height {account.banned_until}")
    _require_balance(state, tx.sender, bond + tx.fee, "the registration bond")
    assert endpoint is not None and engine is not None


def _validate_register_witness(
    params: NetworkParams, state: State, tx: Transaction, height: int
) -> None:
    _check_unknown_keys(tx.payload, ALLOWED[TxType.REGISTER_WITNESS])
    bond = _field_int(tx.payload, "bond")
    if bond < params.clock_witness_bond:
        _fail(
            TxFailure.BOND,
            f"a clock witness needs a bond of at least {params.clock_witness_bond}, got {bond}",
        )
    if tx.sender in state.witnesses:
        _fail(TxFailure.REGISTRATION, f"{tx.sender} is already a registered witness")
    _field_text(tx.payload, "endpoint")
    _require_balance(state, tx.sender, bond + tx.fee, "the witness bond")


def _validate_update_registration(
    params: NetworkParams, state: State, tx: Transaction, height: int
) -> None:
    _check_unknown_keys(tx.payload, ALLOWED[TxType.UPDATE_REGISTRATION])
    account = state.account(tx.sender)
    if account.registered_until_epoch is None:
        _fail(TxFailure.REGISTRATION, f"{tx.sender} is not registered")
    extend = _field_int(tx.payload, "extend_epochs") if "extend_epochs" in tx.payload else 0
    top_up = _field_int(tx.payload, "bond") if "bond" in tx.payload else 0
    if extend == 0 and top_up == 0 and "endpoint" not in tx.payload and "engine" not in tx.payload:
        _fail(TxFailure.TYPE, "update_registration changes nothing")
    for key in ("endpoint", "engine"):
        if key in tx.payload:
            _field_text(tx.payload, key)
    _require_balance(state, tx.sender, top_up + tx.fee, "the bond top-up")


def _validate_unregister(params: NetworkParams, state: State, tx: Transaction, height: int) -> None:
    _check_unknown_keys(tx.payload, ALLOWED[TxType.UNREGISTER])
    account = state.account(tx.sender)
    if account.registered_until_epoch is None:
        _fail(TxFailure.REGISTRATION, f"{tx.sender} is not registered")
    if account.unbonding_height is not None:
        _fail(TxFailure.REGISTRATION, f"{tx.sender} is already unbonding")


def _validate_withdraw_bond(
    params: NetworkParams, state: State, tx: Transaction, height: int
) -> None:
    _check_unknown_keys(tx.payload, ALLOWED[TxType.WITHDRAW_BOND])
    account = state.account(tx.sender)
    if account.unbonding_height is None:
        _fail(TxFailure.REGISTRATION, f"{tx.sender} has not started unbonding")
    if height < account.unbonding_height:
        _fail(
            TxFailure.WINDOW,
            f"bond is locked until height {account.unbonding_height}, this block is {height}",
        )
    if account.bonded == 0:
        _fail(TxFailure.BOND, f"{tx.sender} has no bond to withdraw")


def _validate_availability(
    params: NetworkParams, state: State, tx: Transaction, height: int
) -> None:
    _check_unknown_keys(tx.payload, ALLOWED[TxType.AVAILABILITY])
    epoch = _field_int(tx.payload, "epoch")
    max_games = _field_int(tx.payload, "max_games", minimum=1)
    commitment_hex = _field_digest(tx.payload, "commitment")
    current = epoch_of(height, params)
    expected = current + params.availability_lead
    if epoch != expected:
        _fail(
            TxFailure.WINDOW,
            f"availability for epoch {epoch} belongs in epoch "
            f"{epoch - params.availability_lead}; this block is in epoch {current}",
        )
    if max_games > params.rounds_per_epoch:
        _fail(
            TxFailure.TYPE,
            f"max_games {max_games} exceeds the {params.rounds_per_epoch}-game epoch quota",
        )
    account = state.account(tx.sender)
    if account.registered_until_epoch is None or account.registered_until_epoch < epoch:
        _fail(TxFailure.REGISTRATION, f"{tx.sender} is not registered through epoch {epoch}")
    if account.bonded < params.min_bond:
        _fail(TxFailure.BOND, f"{tx.sender} does not hold the minimum bond")
    existing = state.availability.get(epoch, {}).get(tx.sender)
    if existing is not None and existing.commitment != commitment_hex:
        _fail(TxFailure.DUPLICATE, f"{tx.sender} already committed different availability")


def _validate_reveal(params: NetworkParams, state: State, tx: Transaction, height: int) -> None:
    _check_unknown_keys(tx.payload, ALLOWED[TxType.REVEAL])
    epoch = _field_int(tx.payload, "epoch")
    nonce = _field_digest(tx.payload, "nonce")
    current = epoch_of(height, params)
    if epoch != current + 1:
        _fail(
            TxFailure.WINDOW,
            f"reveal for epoch {epoch} is only valid in epoch {epoch - 1}; "
            f"this block is in epoch {current}",
        )
    committed = state.availability.get(epoch, {}).get(tx.sender)
    if committed is None:
        _fail(TxFailure.REGISTRATION, f"{tx.sender} committed no availability for epoch {epoch}")
    if committed.epoch < first_matchable_epoch(params):
        _fail(TxFailure.WINDOW, f"epoch {epoch} is not matchable")
    if commitment(tx.sender, epoch, nonce) != committed.commitment:
        _fail(TxFailure.TYPE, "reveal nonce does not match the published commitment")
    existing = state.reveals.get(epoch, {}).get(tx.sender)
    if existing is not None and existing != nonce:
        _fail(TxFailure.DUPLICATE, f"{tx.sender} already revealed a different nonce")


def _validate_challenge(params: NetworkParams, state: State, tx: Transaction, height: int) -> None:
    from sccp.errors import ChallengeError
    from sccp.protocol import challenges

    try:
        challenges.validate_challenge(state, tx, height)
    except ChallengeError as exc:
        _fail(TxFailure.TYPE, str(exc))


VALIDATORS = {
    TxType.TRANSFER: _validate_transfer,
    TxType.REGISTER: _validate_register,
    TxType.REGISTER_WITNESS: _validate_register_witness,
    TxType.UPDATE_REGISTRATION: _validate_update_registration,
    TxType.UNREGISTER: _validate_unregister,
    TxType.WITHDRAW_BOND: _validate_withdraw_bond,
    TxType.AVAILABILITY: _validate_availability,
    TxType.REVEAL: _validate_reveal,
    TxType.CHALLENGE: _validate_challenge,
}
"""Handlers for every type that :func:`validate_transaction` owns outright."""


# -- application --------------------------------------------------------------


def _apply_transfer(state: State, tx: Transaction, height: int) -> None:
    amount = _field_int(tx.payload, "amount", minimum=1)
    to = tx.payload.get("to")
    if not isinstance(to, str):
        _fail(TxFailure.SCHEMA, "payload field 'to' must be an address")
    state.ensure(tx.sender).balance -= amount
    state.ensure(to).balance += amount


def _apply_register(state: State, tx: Transaction, height: int) -> None:
    bond = _field_int(tx.payload, "bond")
    account = state.ensure(tx.sender)
    account.balance -= bond
    account.bonded += bond
    account.endpoint = _field_text(tx.payload, "endpoint")
    account.engine = _field_text(tx.payload, "engine")
    account.registered_until_epoch = (
        epoch_of(height, state.params) + state.params.registration_epochs
    )


def _apply_register_witness(state: State, tx: Transaction, height: int) -> None:
    from sccp.chain.state import WitnessRecord

    bond = _field_int(tx.payload, "bond")
    endpoint = _field_text(tx.payload, "endpoint") or ""
    account = state.ensure(tx.sender)
    account.balance -= bond
    account.bonded += bond
    state.witnesses[tx.sender] = WitnessRecord(
        public_key=account.public_key or "",
        endpoint=endpoint,
        bond=bond,
        registered_at_height=height,
        until_epoch=epoch_of(height, state.params) + state.params.registration_epochs,
    )


def _apply_update_registration(state: State, tx: Transaction, height: int) -> None:
    account = state.ensure(tx.sender)
    if "extend_epochs" in tx.payload:
        account.registered_until_epoch = (account.registered_until_epoch or 0) + _field_int(
            tx.payload, "extend_epochs"
        )
    if "bond" in tx.payload:
        top_up = _field_int(tx.payload, "bond")
        account.balance -= top_up
        account.bonded += top_up
    if "endpoint" in tx.payload:
        account.endpoint = _field_text(tx.payload, "endpoint")
    if "engine" in tx.payload:
        account.engine = _field_text(tx.payload, "engine")


def _apply_unregister(state: State, tx: Transaction, height: int) -> None:
    account = state.ensure(tx.sender)
    current = epoch_of(height, state.params)
    if account.registered_until_epoch is not None:
        account.registered_until_epoch = min(account.registered_until_epoch, current)
    account.unbonding_height = height + state.params.unbond_delay


def _apply_withdraw_bond(state: State, tx: Transaction, height: int) -> None:
    account = state.ensure(tx.sender)
    account.balance += account.bonded
    account.bonded = 0
    account.unbonding_height = None
    account.registered_until_epoch = None
    state.witnesses.pop(tx.sender, None)


def _apply_availability(state: State, tx: Transaction, height: int) -> None:
    epoch = _field_int(tx.payload, "epoch")
    max_games = _field_int(tx.payload, "max_games", minimum=1)
    entry = Availability(
        network=state.network,
        miner=tx.sender,
        epoch=epoch,
        max_games=max_games,
        commitment=_field_digest(tx.payload, "commitment"),
    )
    state.availability.setdefault(epoch, {})[tx.sender] = entry


def _apply_reveal(state: State, tx: Transaction, height: int) -> None:
    epoch = _field_int(tx.payload, "epoch")
    state.reveals.setdefault(epoch, {})[tx.sender] = _field_digest(tx.payload, "nonce")


def _apply_timeout_claim(state: State, tx: Transaction, height: int) -> None:
    from sccp.chain import timeout

    record = timeout.validate_claim(state, tx, height)
    state.ensure(tx.sender).balance -= record.bond
    state.timeout_claims[record.claim_id] = record


def _apply_timeout_dispute(state: State, tx: Transaction, height: int) -> None:
    from sccp.chain import timeout

    timeout.apply_dispute(state, tx, height)


def _apply_slash_evidence(state: State, tx: Transaction, height: int) -> None:
    from sccp.chain import timeout

    timeout.apply_slash(state, tx, height)


def _apply_challenge(state: State, tx: Transaction, height: int) -> None:
    from sccp.errors import ChallengeError
    from sccp.protocol import challenges

    try:
        challenges.apply_challenge(state, tx, height)
    except ChallengeError as exc:
        _fail(TxFailure.TYPE, str(exc))


APPLIERS = {
    TxType.TRANSFER: _apply_transfer,
    TxType.REGISTER: _apply_register,
    TxType.REGISTER_WITNESS: _apply_register_witness,
    TxType.UPDATE_REGISTRATION: _apply_update_registration,
    TxType.UNREGISTER: _apply_unregister,
    TxType.WITHDRAW_BOND: _apply_withdraw_bond,
    TxType.AVAILABILITY: _apply_availability,
    TxType.REVEAL: _apply_reveal,
    TxType.TIMEOUT_CLAIM: _apply_timeout_claim,
    TxType.TIMEOUT_DISPUTE: _apply_timeout_dispute,
    TxType.CHALLENGE: _apply_challenge,
    TxType.SLASH_EVIDENCE: _apply_slash_evidence,
}
"""State transitions for every accepted transaction type."""


# -- public API ---------------------------------------------------------------


def validate_transaction(
    tx: Transaction,
    params: NetworkParams,
    state: State,
    *,
    height: int,
    for_mempool: bool = False,
) -> None:
    """Run the full transaction checklist for ``tx`` at ``height``.

    ``for_mempool`` additionally rejects a transaction that is still valid on
    chain but already superseded in the pool; the pool itself enforces its own
    ``(sender, nonce)`` uniqueness, so the flag only documents intent.
    """
    if not isinstance(tx, Transaction):
        _fail(TxFailure.SCHEMA, f"expected a Transaction, got {type(tx).__name__}")
    if tx.network != params.network_id:
        _fail(
            TxFailure.NETWORK,
            f"transaction is for {tx.network!r}, this node is on {params.network_id!r}",
        )
    if not validate_address(tx.sender, params):
        _fail(TxFailure.SCHEMA, f"{tx.sender!r} is not a {params.network_id} address")
    if not isinstance(tx.payload, dict):
        _fail(TxFailure.SCHEMA, "payload must be an object")

    # Signature before nonce: a forger must not learn whether a nonce is free.
    key_hex = check_sender_key(state, params, tx)

    account = state.account(tx.sender)
    if tx.nonce < account.nonce:
        _fail(
            TxFailure.NONCE,
            f"nonce {tx.nonce} was already used; {tx.sender} is at nonce {account.nonce}",
        )
    if tx.nonce > account.nonce:
        _fail(
            TxFailure.NONCE,
            f"nonce {tx.nonce} is ahead of {account.nonce}; transactions must apply in order",
        )

    if tx.fee < params.min_fee:
        _fail(TxFailure.FEE, f"fee {tx.fee} is below the {params.min_fee}-unit minimum")

    handler = VALIDATORS.get(tx.type)
    if handler is not None:
        handler(params, state, tx, height)
    elif tx.type is TxType.TIMEOUT_CLAIM:
        from sccp.chain import timeout

        timeout.validate_claim(state, tx, height)
    elif tx.type is TxType.TIMEOUT_DISPUTE:
        from sccp.chain import timeout

        timeout.validate_dispute(state, tx, height)
    elif tx.type is TxType.SLASH_EVIDENCE:
        from sccp.chain import timeout

        timeout.validate_slash(state, tx, height)
    else:  # pragma: no cover - the enum is exhaustive
        _fail(TxFailure.TYPE, f"unsupported transaction type {tx.type!r}")

    size = tx_size(tx)
    if size > params.max_tx_bytes:
        _fail(TxFailure.SIZE, f"transaction is {size} bytes, cap is {params.max_tx_bytes}")

    if account.public_key is None and tx.payload.get("public_key") != key_hex:
        _fail(TxFailure.SIGNATURE, "first use must reveal the sender's public key")


def apply_transaction(state: State, tx: Transaction, *, height: int) -> None:
    """Apply a validated transaction to ``state``.

    Callers must have run :func:`validate_transaction` against the same state and
    height; this function assumes every check passed and only mutates.
    """
    account = state.ensure(tx.sender)
    account.balance -= tx.fee
    account.nonce += 1
    if account.public_key is None:
        account.public_key = resolve_public_key(state, tx)
    applier = APPLIERS.get(tx.type)
    if applier is None:
        _fail(TxFailure.TYPE, f"no state transition for {tx.type!r}")
    applier(state, tx, height)


def transaction_fee_total(transactions: tuple[Transaction, ...] | list[Transaction]) -> int:
    """Sum of the fees a block collects."""
    return sum(tx.fee for tx in transactions)


__all__ = [
    "ALLOWED",
    "APPLIERS",
    "MAX_SAFE_TEXT",
    "VALIDATORS",
    "TxFailure",
    "apply_transaction",
    "check_sender_key",
    "coinbase_id",
    "make_coinbase",
    "resolve_public_key",
    "sign_transaction",
    "transaction_fee_total",
    "tx_body",
    "tx_size",
    "txid",
    "validate_transaction",
]
