"""Bonded timeout claims, disputes, finalisation and witness slashing (plan 5.7.1)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from sccp.chain import timeout
from sccp.chain.state import State, WitnessRecord
from sccp.chain.tx import apply_transaction, sign_transaction, txid, validate_transaction
from sccp.config import DEVNET, NetworkParams
from sccp.errors import TimeoutClaimError
from sccp.protocol.clocks import (
    FIXED_TIME_CONTROL,
    move_payload,
    sign_move,
    sign_receipt,
)
from sccp.protocol.models import (
    ClockEvidence,
    FlagReceipt,
    MoveReceipt,
    SignedMove,
    Transaction,
    TxType,
)

COINS = 100_000_000
HEIGHT = 10
BOND = DEVNET.min_timeout_bond


@pytest.fixture()
def timeout_state(protocol_params: NetworkParams, protocol_accounts: Mapping[str, Any]) -> State:
    """A ledger with the two seats and one bonded clock witness (Carol)."""
    state = State(params=protocol_params, height=HEIGHT, timestamps=[1_700_000_000])
    for account in protocol_accounts.values():
        entry = state.ensure(account.address)
        entry.public_key = account.keypair.public_key_hex
        entry.balance = 100 * COINS
    carol = state.ensure(protocol_accounts["carol"].address)
    carol.bonded = protocol_params.clock_witness_bond
    carol.balance -= protocol_params.clock_witness_bond
    state.witnesses[protocol_accounts["carol"].address] = WitnessRecord(
        public_key=protocol_accounts["carol"].keypair.public_key_hex,
        endpoint="tcp://carol.test",
        bond=protocol_params.clock_witness_bond,
        registered_at_height=0,
        until_epoch=1_000,
    )
    return state


def _last_move(match: Any) -> SignedMove:
    """The claimant's (white's) final move, signed by the seat that played it."""
    certificate = match.certificate
    message = sign_move(
        SignedMove(
            network=certificate.network,
            match_id=certificate.match_id,
            seat=certificate.white,
            ply=len(certificate.opening.moves),
            san="Qh5",
            signature="00" * 64,
        ),
        match.accounts["alice"].keypair,
    )
    return SignedMove.model_validate(move_payload(message) | {"signature": message.signature})


def _flag_evidence(
    match: Any, *, accused_ms: int = 0, ply_offset: int = 0, signer: str = "carol"
) -> ClockEvidence:
    """A flag receipt for black, the seat that is not the claimant."""
    certificate = match.certificate
    receipt = sign_receipt(
        FlagReceipt(
            match_id=certificate.match_id,
            ply=len(certificate.opening.moves) + 1 + ply_offset,
            seat=certificate.black,
            received_at_ms=1_000_000,
            white_ms=1_500_000,
            black_ms=accused_ms,
        ),
        match.accounts[signer].keypair,
        network=certificate.network,
    )
    return ClockEvidence(time_control=FIXED_TIME_CONTROL, final=receipt)


def _claim_tx(
    match: Any,
    *,
    evidence: ClockEvidence,
    bond: int = BOND,
    claimant: str = "alice",
    last_move: SignedMove | None = None,
    nonce: int = 0,
) -> Transaction:
    certificate = match.certificate
    account = match.accounts[claimant]
    move = last_move or _last_move(match)
    tx = Transaction(
        network=certificate.network,
        type=TxType.TIMEOUT_CLAIM,
        sender=account.address,
        nonce=nonce,
        fee=DEVNET.min_fee,
        payload={
            "match_id": certificate.match_id,
            "certificate": certificate.model_dump(mode="json", by_alias=True),
            "last_move": move_payload(move) | {"signature": move.signature},
            "clock_evidence": evidence.model_dump(mode="json", by_alias=True),
            "bond": bond,
            "public_key": account.keypair.public_key_hex,
        },
    )
    return sign_transaction(account.keypair, tx)


def test_a_valid_claim_opens_a_bonded_record(signed_match: Any, timeout_state: State) -> None:
    evidence = _flag_evidence(signed_match)
    tx = _claim_tx(signed_match, evidence=evidence)
    validate_transaction(tx, timeout_state.params, timeout_state, height=HEIGHT)
    apply_transaction(timeout_state, tx, height=HEIGHT)

    record = timeout_state.timeout_claims[txid(tx)]
    certificate = signed_match.certificate
    assert record.claimant == certificate.white
    assert record.accused == certificate.black
    assert record.match_id == certificate.match_id
    assert record.bond == BOND
    assert record.opened_height == HEIGHT
    assert record.deadline_height == HEIGHT + DEVNET.timeout_dispute_window
    assert record.open is True
    account = timeout_state.account(certificate.white)
    assert account.balance == 100 * COINS - DEVNET.min_fee - BOND
    assert account.nonce == 1


def test_a_claim_must_be_bonded_and_come_from_a_seat(
    signed_match: Any, timeout_state: State
) -> None:
    thin = _claim_tx(signed_match, evidence=_flag_evidence(signed_match), bond=BOND - 1)
    with pytest.raises(TimeoutClaimError, match="bond of at least"):
        timeout.validate_claim(timeout_state, thin, HEIGHT)

    outsider = _claim_tx(signed_match, evidence=_flag_evidence(signed_match), claimant="carol")
    with pytest.raises(TimeoutClaimError, match="not a seat"):
        timeout.validate_claim(timeout_state, outsider, HEIGHT)


def test_a_claim_needs_a_zero_clock_and_the_next_ply(
    signed_match: Any, timeout_state: State
) -> None:
    alive = _claim_tx(signed_match, evidence=_flag_evidence(signed_match, accused_ms=1_000))
    with pytest.raises(TimeoutClaimError, match="still has 1000 ms"):
        timeout.validate_claim(timeout_state, alive, HEIGHT)

    late = _claim_tx(signed_match, evidence=_flag_evidence(signed_match, ply_offset=1))
    with pytest.raises(TimeoutClaimError, match="expected"):
        timeout.validate_claim(timeout_state, late, HEIGHT)


def test_a_claim_needs_a_registered_witness_signature(
    signed_match: Any, timeout_state: State
) -> None:
    forged = _claim_tx(signed_match, evidence=_flag_evidence(signed_match, signer="alice"))
    with pytest.raises(TimeoutClaimError, match="witness verification"):
        timeout.validate_claim(timeout_state, forged, HEIGHT)


def test_one_open_claim_per_match(signed_match: Any, timeout_state: State) -> None:
    first = _claim_tx(signed_match, evidence=_flag_evidence(signed_match))
    validate_transaction(first, timeout_state.params, timeout_state, height=HEIGHT)
    apply_transaction(timeout_state, first, height=HEIGHT)

    second = _claim_tx(signed_match, evidence=_flag_evidence(signed_match), nonce=1)
    with pytest.raises(TimeoutClaimError, match="already has an open timeout claim"):
        timeout.validate_claim(timeout_state, second, HEIGHT)


def test_unknown_claim_fields_are_rejected(signed_match: Any) -> None:
    tx = _claim_tx(signed_match, evidence=_flag_evidence(signed_match))
    assert tx.payload is not None
    tx = tx.model_copy(update={"payload": {**tx.payload, "note": "hi"}})
    with pytest.raises(TimeoutClaimError, match="unknown timeout_claim payload fields"):
        timeout.parse_claim(tx)


def _dispute_tx(
    match: Any,
    claim_id: str,
    *,
    evidence: ClockEvidence,
    signer: str = "bob",
    ply: int | None = None,
    nonce: int = 0,
    signature: str | None = None,
) -> Transaction:
    """The accused's answer: a later move of its own."""
    certificate = match.certificate
    account = match.accounts[signer]
    message = sign_move(
        SignedMove(
            network=certificate.network,
            match_id=certificate.match_id,
            seat=certificate.black,
            ply=ply if ply is not None else len(certificate.opening.moves) + 1,
            san="Nf6",
            signature="00" * 64,
        ),
        account.keypair,
    )
    tx = Transaction(
        network=certificate.network,
        type=TxType.TIMEOUT_DISPUTE,
        sender=account.address,
        nonce=nonce,
        fee=DEVNET.min_fee,
        payload={
            "claim_id": claim_id,
            "move": move_payload(message) | {"signature": signature or message.signature},
            "move_signature": signature or message.signature,
            "clock_evidence": evidence.model_dump(mode="json", by_alias=True),
            "public_key": account.keypair.public_key_hex,
        },
    )
    return sign_transaction(account.keypair, tx)


def _receipts(match: Any, *, first_san: str = "Nf6", second_san: str = "Nc6") -> list[Any]:
    """Two conflicting move receipts, same match and ply, both signed by Carol."""
    certificate = match.certificate
    carol = match.accounts["carol"].keypair
    receipts = []
    for san in (first_san, second_san):
        receipts.append(
            sign_receipt(
                MoveReceipt(
                    match_id=certificate.match_id,
                    ply=len(certificate.opening.moves),
                    seat=certificate.black,
                    san=san,
                    received_at_ms=1_000_000,
                    white_ms=1_500_000,
                    black_ms=1_400_000,
                ),
                carol,
                network=certificate.network,
            )
        )
    return receipts


def _slash_tx(match: Any, receipts: list[Any], *, witness: str = "carol") -> Transaction:
    carol = match.accounts["carol"]
    bob = match.accounts["bob"]
    evidence = {
        "witness": carol.address if witness == "carol" else match.accounts[witness].address,
        "receipts": [receipt.model_dump(mode="json", by_alias=True) for receipt in receipts],
    }
    tx = Transaction(
        network=match.certificate.network,
        type=TxType.SLASH_EVIDENCE,
        sender=bob.address,
        nonce=0,
        fee=DEVNET.min_fee,
        payload={
            "evidence": evidence,
            "public_key": bob.keypair.public_key_hex,
        },
    )
    return sign_transaction(bob.keypair, tx)


@pytest.fixture()
def open_claim(signed_match: Any, timeout_state: State) -> tuple[State, Any, str]:
    """A state with a valid, applied timeout claim at :data:`HEIGHT`."""
    tx = _claim_tx(signed_match, evidence=_flag_evidence(signed_match))
    validate_transaction(tx, timeout_state.params, timeout_state, height=HEIGHT)
    apply_transaction(timeout_state, tx, height=HEIGHT)
    return timeout_state, signed_match, txid(tx)


def test_the_accused_dispute_returns_the_bond(open_claim: tuple[State, Any, str]) -> None:
    state, match, claim_id = open_claim
    record = state.timeout_claims[claim_id]
    assert record.accused == match.certificate.black
    before = state.account(record.claimant).balance

    tx = _dispute_tx(match, claim_id, evidence=_flag_evidence(match))
    validate_transaction(tx, state.params, state, height=HEIGHT + 1)
    apply_transaction(state, tx, height=HEIGHT + 1)

    assert record.disputed_height == HEIGHT + 1
    assert record.open is False
    assert state.account(record.claimant).balance == before + record.bond
    with pytest.raises(TimeoutClaimError, match="was disputed"):
        timeout.finalise_claim(state, claim_id, HEIGHT + DEVNET.timeout_dispute_window + 1)


def test_only_the_accused_may_dispute(open_claim: tuple[State, Any, str]) -> None:
    state, match, claim_id = open_claim
    tx = _dispute_tx(match, claim_id, evidence=_flag_evidence(match), signer="alice")
    with pytest.raises(TimeoutClaimError, match="only the accused seat"):
        timeout.validate_dispute(state, tx, HEIGHT + 1)


def test_a_dispute_must_be_timely_and_later(open_claim: tuple[State, Any, str]) -> None:
    state, match, claim_id = open_claim
    evidence = _flag_evidence(match)
    late = HEIGHT + DEVNET.timeout_dispute_window + 1
    with pytest.raises(TimeoutClaimError, match="closed at height"):
        timeout.validate_dispute(state, _dispute_tx(match, claim_id, evidence=evidence), late)

    stale = len(match.certificate.opening.moves)
    with pytest.raises(TimeoutClaimError, match="not after the claimed last move"):
        timeout.validate_dispute(
            state, _dispute_tx(match, claim_id, evidence=evidence, ply=stale), HEIGHT + 1
        )

    forged = _dispute_tx(match, claim_id, evidence=evidence, signature="00" * 64)
    with pytest.raises(TimeoutClaimError, match="does not verify"):
        timeout.validate_dispute(state, forged, HEIGHT + 1)


def test_finalising_releases_the_bond_after_the_window(open_claim: tuple[State, Any, str]) -> None:
    state, _match, claim_id = open_claim
    record = state.timeout_claims[claim_id]
    with pytest.raises(TimeoutClaimError, match="can only finalise after height"):
        timeout.finalise_claim(state, claim_id, record.deadline_height)
    assert record.open is True

    before = state.account(record.claimant).balance
    closed = timeout.finalise_claim(state, claim_id, record.deadline_height + 1)
    assert closed.finalised_height == record.deadline_height + 1
    assert closed.open is False
    assert state.account(record.claimant).balance == before + record.bond

    again = timeout.finalise_claim(state, claim_id, record.deadline_height + 2)
    assert again.finalised_height == record.deadline_height + 1
    assert state.account(record.claimant).balance == before + record.bond


def test_finalising_an_unknown_claim_fails(signed_match: Any, timeout_state: State) -> None:
    with pytest.raises(TimeoutClaimError, match="no timeout claim"):
        timeout.finalise_claim(timeout_state, "00" * 32, HEIGHT + 10_000)


def test_an_equivocating_witness_loses_its_bond(signed_match: Any, timeout_state: State) -> None:
    tx = _slash_tx(signed_match, _receipts(signed_match))
    validate_transaction(tx, timeout_state.params, timeout_state, height=HEIGHT)
    apply_transaction(timeout_state, tx, height=HEIGHT)

    address = signed_match.accounts["carol"].address
    assert address not in timeout_state.witnesses
    assert timeout_state.account(address).bonded == 0
    assert timeout_state.account(address).banned_until == HEIGHT + DEVNET.unbond_delay


def test_slash_needs_two_different_receipts_from_a_registered_witness(
    signed_match: Any, timeout_state: State
) -> None:
    identical = _receipts(signed_match, first_san="Nf6", second_san="Nf6")
    with pytest.raises(TimeoutClaimError, match="identical"):
        timeout.validate_slash(timeout_state, _slash_tx(signed_match, identical), HEIGHT)

    stranger = _slash_tx(signed_match, _receipts(signed_match), witness="alice")
    with pytest.raises(TimeoutClaimError, match="not a registered clock witness"):
        timeout.validate_slash(timeout_state, stranger, HEIGHT)


def test_slash_rejects_receipts_for_different_plies(
    signed_match: Any, timeout_state: State
) -> None:
    receipts = _receipts(signed_match)
    later = receipts[1].model_copy(update={"ply": receipts[1].ply + 1})
    with pytest.raises(TimeoutClaimError, match="same match, kind and ply"):
        timeout.validate_slash(timeout_state, _slash_tx(signed_match, [receipts[0], later]), HEIGHT)


def test_slash_is_only_about_clock_witnesses_for_now(
    signed_match: Any, timeout_state: State
) -> None:
    tx = _slash_tx(signed_match, _receipts(signed_match))
    assert tx.payload is not None
    unsupported = tx.model_copy(update={"payload": {**tx.payload, "proof_id": "11" * 32}})
    with pytest.raises(TimeoutClaimError, match="not a timeout rule"):
        timeout.validate_slash(timeout_state, unsupported, HEIGHT)

    unknown = tx.model_copy(update={"payload": {**tx.payload, "note": "hi"}})
    with pytest.raises(TimeoutClaimError, match="unknown slash_evidence payload fields"):
        timeout.validate_slash(timeout_state, unknown, HEIGHT)
