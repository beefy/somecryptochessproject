"""Transactions: the plan-5.4 checklist and its state transitions."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from sccp.chain.state import State
from sccp.chain.tx import (
    TxFailure,
    apply_transaction,
    sign_transaction,
    tx_size,
    txid,
    validate_transaction,
)
from sccp.config import DEVNET, NetworkParams
from sccp.errors import TxValidationError
from sccp.protocol.models import Transaction, TxType
from sccp.wallet.service import WalletService

HEIGHT = 5
START = 1_700_000_000
COINS = 100_000_000


def _ledger(protocol_params: NetworkParams, protocol_accounts: Mapping[str, Any]) -> State:
    """Devnet state one block before :data:`HEIGHT` with funded, keyed accounts."""
    state = State(params=protocol_params, height=HEIGHT - 1, timestamps=[START])
    for account in protocol_accounts.values():
        entry = state.ensure(account.address)
        entry.public_key = account.keypair.public_key_hex
        entry.balance = 1_000 * COINS
        entry.registered_until_epoch = 1_000
    return state


@pytest.fixture()
def ledger(protocol_params: NetworkParams, protocol_accounts: Mapping[str, Any]) -> State:
    return _ledger(protocol_params, protocol_accounts)


def _params(**overrides: Any) -> NetworkParams:
    return DEVNET.model_copy(update=overrides)


def _wallet(state: State, params: NetworkParams = DEVNET) -> WalletService:
    return WalletService(params, state)


def _resigned(tx: Transaction, account: Any) -> Transaction:
    return sign_transaction(account.keypair, tx.model_copy(update={"signature": None}))


def _check(
    tx: Transaction, state: State, params: NetworkParams = DEVNET, height: int = HEIGHT
) -> None:
    validate_transaction(tx, params, state, height=height)


def _reason(tx: Transaction, state: State, **kwargs: Any) -> TxFailure:
    with pytest.raises(TxValidationError) as excinfo:
        _check(tx, state, **kwargs)
    return excinfo.value.reason


def test_transfer_moves_funds_and_charges_the_fee(
    ledger: State, protocol_accounts: Mapping[str, Any]
) -> None:
    alice, bob = protocol_accounts["alice"], protocol_accounts["bob"]
    tx = _wallet(ledger).build_transfer(alice.keypair, bob.address, 100)
    _check(tx, ledger)
    before = ledger.account(alice.address).balance
    apply_transaction(ledger, tx, height=HEIGHT)
    assert ledger.account(alice.address).balance == before - 100 - DEVNET.min_fee
    assert ledger.account(bob.address).balance == 1_000 * COINS + 100
    assert ledger.account(alice.address).nonce == 1


def test_transfer_rejects_a_stale_nonce(
    ledger: State, protocol_accounts: Mapping[str, Any]
) -> None:
    alice, bob = protocol_accounts["alice"], protocol_accounts["bob"]
    tx = _wallet(ledger).build_transfer(alice.keypair, bob.address, 100)
    apply_transaction(ledger, tx, height=HEIGHT)
    replay = _resigned(tx.model_copy(update={"nonce": 0}), alice)
    assert _reason(replay, ledger) is TxFailure.NONCE


def test_transfer_rejects_a_nonce_from_the_future(
    ledger: State, protocol_accounts: Mapping[str, Any]
) -> None:
    alice, bob = protocol_accounts["alice"], protocol_accounts["bob"]
    tx = _wallet(ledger).build_transfer(alice.keypair, bob.address, 100)
    ahead = _resigned(tx.model_copy(update={"nonce": 7}), alice)
    assert _reason(ahead, ledger) is TxFailure.NONCE


def test_transfer_requires_funds(ledger: State, protocol_accounts: Mapping[str, Any]) -> None:
    alice, bob = protocol_accounts["alice"], protocol_accounts["bob"]
    tx = _wallet(ledger).build_transfer(alice.keypair, bob.address, 1)
    huge = _resigned(tx.model_copy(update={"payload": {**tx.payload, "amount": 10**15}}), alice)
    assert _reason(huge, ledger) is TxFailure.BALANCE


def test_transfer_rejects_self_payment(ledger: State, protocol_accounts: Mapping[str, Any]) -> None:
    alice = protocol_accounts["alice"]
    tx = _wallet(ledger).build_transfer(alice.keypair, protocol_accounts["bob"].address, 1)
    selfish = _resigned(
        tx.model_copy(update={"payload": {**tx.payload, "to": alice.address}}), alice
    )
    assert _reason(selfish, ledger) is TxFailure.TYPE


def test_transfer_rejects_unknown_payload_keys(
    ledger: State, protocol_accounts: Mapping[str, Any]
) -> None:
    alice, bob = protocol_accounts["alice"], protocol_accounts["bob"]
    tx = _wallet(ledger).build_transfer(alice.keypair, bob.address, 1)
    extra = _resigned(tx.model_copy(update={"payload": {**tx.payload, "memo": "hi"}}), alice)
    assert _reason(extra, ledger) is TxFailure.SCHEMA


def test_transfer_rejects_a_bad_recipient(
    ledger: State, protocol_accounts: Mapping[str, Any]
) -> None:
    alice = protocol_accounts["alice"]
    tx = _wallet(ledger).build_transfer(alice.keypair, protocol_accounts["bob"].address, 1)
    typo = _resigned(tx.model_copy(update={"payload": {**tx.payload, "to": "sccpt1nope"}}), alice)
    assert _reason(typo, ledger) is TxFailure.SCHEMA


def test_unsigned_and_foreign_transactions_are_rejected(
    ledger: State, protocol_accounts: Mapping[str, Any]
) -> None:
    alice, bob = protocol_accounts["alice"], protocol_accounts["bob"]
    tx = _wallet(ledger).build_transfer(alice.keypair, bob.address, 1)
    assert _reason(tx.model_copy(update={"signature": None}), ledger) is TxFailure.SIGNATURE
    assert _reason(tx.model_copy(update={"network": "testnet"}), ledger) is TxFailure.NETWORK


def test_fee_must_meet_the_network_minimum(
    ledger: State, protocol_accounts: Mapping[str, Any]
) -> None:
    alice, bob = protocol_accounts["alice"], protocol_accounts["bob"]
    tx = _wallet(ledger).build_transfer(alice.keypair, bob.address, 1)
    cheap = _resigned(tx.model_copy(update={"fee": DEVNET.min_fee - 1}), alice)
    assert _reason(cheap, ledger) is TxFailure.FEE


def test_first_use_must_reveal_the_public_key(
    ledger: State, protocol_accounts: Mapping[str, Any]
) -> None:
    alice, bob = protocol_accounts["alice"], protocol_accounts["bob"]
    ledger.accounts[alice.address].public_key = None
    tx = _wallet(ledger).build_transfer(alice.keypair, bob.address, 1)
    _check(tx, ledger)
    silent = _resigned(
        tx.model_copy(
            update={
                "payload": {key: value for key, value in tx.payload.items() if key != "public_key"}
            }
        ),
        alice,
    )
    assert _reason(silent, ledger) is TxFailure.SIGNATURE


def test_register_locks_the_bond_and_sets_the_registration_window(
    ledger: State, protocol_accounts: Mapping[str, Any]
) -> None:
    alice = protocol_accounts["alice"]
    params = _params(min_bond=1_000)
    tx = _wallet(ledger, params).build_register(
        alice.keypair, endpoint="tcp://alice.test", engine="mock-v1", bond=5_000
    )
    _check(tx, ledger, params=params)
    apply_transaction(ledger, tx, height=HEIGHT)
    account = ledger.account(alice.address)
    assert account.bonded == 5_000
    assert account.endpoint == "tcp://alice.test"
    assert account.engine == "mock-v1"
    assert account.registered_until_epoch == HEIGHT + DEVNET.registration_epochs


def test_register_below_the_minimum_bond_is_rejected(
    ledger: State, protocol_accounts: Mapping[str, Any]
) -> None:
    alice = protocol_accounts["alice"]
    params = _params(min_bond=1_000)
    tx = _wallet(ledger, params).build_register(
        alice.keypair, endpoint="tcp://alice.test", engine="mock-v1", bond=1_000
    )
    short = _resigned(tx.model_copy(update={"payload": {**tx.payload, "bond": 999}}), alice)
    assert _reason(short, ledger, params=params) is TxFailure.BOND


def test_unregister_then_withdraw_bond_after_the_delay(
    ledger: State, protocol_accounts: Mapping[str, Any]
) -> None:
    alice = protocol_accounts["alice"]
    params = _params(min_bond=1_000)
    wallet = _wallet(ledger, params)
    register = wallet.build_register(
        alice.keypair, endpoint="tcp://alice.test", engine="mock-v1", bond=5_000
    )
    apply_transaction(ledger, register, height=HEIGHT)

    def retype(tx: Transaction, kind: TxType, payload: dict[str, object]) -> Transaction:
        nonce = ledger.account(alice.address).nonce
        return _resigned(
            tx.model_copy(update={"type": kind, "nonce": nonce, "payload": payload}), alice
        )

    key = {"public_key": alice.keypair.public_key_hex}
    leave = retype(register, TxType.UNREGISTER, key)
    _check(leave, ledger, params=params)
    apply_transaction(ledger, leave, height=HEIGHT)
    account = ledger.account(alice.address)
    assert account.unbonding_height == HEIGHT + DEVNET.unbond_delay
    assert account.registered_until_epoch == HEIGHT // DEVNET.epoch_length

    withdraw = retype(register, TxType.WITHDRAW_BOND, key)
    assert _reason(withdraw, ledger, params=params) is TxFailure.WINDOW

    unlock = HEIGHT + DEVNET.unbond_delay
    _check(withdraw, ledger, params=params, height=unlock)
    balance = ledger.account(alice.address).balance
    apply_transaction(ledger, withdraw, height=unlock)
    account = ledger.account(alice.address)
    assert account.bonded == 0
    assert account.unbonding_height is None
    assert account.registered_until_epoch is None
    assert account.balance == balance - DEVNET.min_fee + 5_000


def test_availability_is_bound_to_its_commit_epoch(
    ledger: State, protocol_accounts: Mapping[str, Any]
) -> None:
    alice = protocol_accounts["alice"]
    wallet = _wallet(ledger)
    tx = wallet.build_availability(alice.keypair, nonce_secret="11" * 32, max_games=2)
    assert tx.payload["epoch"] == HEIGHT + DEVNET.availability_lead
    _check(tx, ledger)
    apply_transaction(ledger, tx, height=HEIGHT)
    assert ledger.availability[HEIGHT + DEVNET.availability_lead][alice.address].max_games == 2

    early = _resigned(
        tx.model_copy(update={"nonce": 1, "payload": {**tx.payload, "epoch": HEIGHT + 1}}), alice
    )
    assert _reason(early, ledger) is TxFailure.WINDOW
    greedy = _resigned(
        tx.model_copy(
            update={
                "nonce": 1,
                "payload": {**tx.payload, "max_games": DEVNET.rounds_per_epoch + 1},
            }
        ),
        alice,
    )
    assert _reason(greedy, ledger) is TxFailure.TYPE


def test_availability_cannot_be_changed_once_committed(
    ledger: State, protocol_accounts: Mapping[str, Any]
) -> None:
    alice = protocol_accounts["alice"]
    wallet = _wallet(ledger)
    first = wallet.build_availability(alice.keypair, nonce_secret="11" * 32)
    apply_transaction(ledger, first, height=HEIGHT)
    second = wallet.build_availability(alice.keypair, nonce_secret="22" * 32)
    assert _reason(second, ledger) is TxFailure.DUPLICATE


def test_reveal_needs_a_matching_commitment(
    ledger: State, protocol_accounts: Mapping[str, Any]
) -> None:
    alice = protocol_accounts["alice"]
    wallet = _wallet(ledger)
    with pytest.raises(TxValidationError) as excinfo:
        _check(wallet.build_reveal(alice.keypair, "11" * 32), ledger)
    assert excinfo.value.reason is TxFailure.REGISTRATION

    commit = wallet.build_availability(alice.keypair, nonce_secret="11" * 32)
    apply_transaction(ledger, commit, height=HEIGHT)
    ledger.height = HEIGHT
    reveal = wallet.build_reveal(alice.keypair, "11" * 32)
    assert reveal.payload["epoch"] == HEIGHT + DEVNET.availability_lead
    _check(reveal, ledger, height=HEIGHT + 1)
    apply_transaction(ledger, reveal, height=HEIGHT + 1)
    assert ledger.reveals[HEIGHT + DEVNET.availability_lead][alice.address] == "11" * 32


def test_reveal_rejects_a_wrong_nonce_and_a_wrong_window(
    ledger: State, protocol_accounts: Mapping[str, Any]
) -> None:
    alice = protocol_accounts["alice"]
    wallet = _wallet(ledger)
    commit = wallet.build_availability(alice.keypair, nonce_secret="11" * 32)
    apply_transaction(ledger, commit, height=HEIGHT)
    ledger.height = HEIGHT
    reveal = wallet.build_reveal(alice.keypair, "11" * 32)
    wrong = _resigned(
        reveal.model_copy(update={"payload": {**reveal.payload, "nonce": "22" * 32}}),
        alice,
    )
    assert _reason(wrong, ledger, height=HEIGHT + 1) is TxFailure.TYPE
    late = _resigned(
        wrong.model_copy(
            update={"payload": {**wrong.payload, "nonce": "11" * 32, "epoch": HEIGHT}}
        ),
        alice,
    )
    assert _reason(late, ledger) is TxFailure.WINDOW


def test_quality_challenges_are_explicitly_out_of_scope(
    ledger: State, protocol_accounts: Mapping[str, Any]
) -> None:
    alice = protocol_accounts["alice"]
    tx = _wallet(ledger).build_transfer(alice.keypair, protocol_accounts["bob"].address, 1)
    challenge = _resigned(
        tx.model_copy(
            update={
                "type": TxType.CHALLENGE,
                "payload": {"proof_id": "00" * 32, "reason": "bad", "bond": 1},
            }
        ),
        alice,
    )
    with pytest.raises(TxValidationError, match="M5"):
        _check(challenge, ledger)


def test_txid_ignores_the_signature_and_tracks_the_body(
    ledger: State, protocol_accounts: Mapping[str, Any]
) -> None:
    alice, bob = protocol_accounts["alice"], protocol_accounts["bob"]
    tx = _wallet(ledger).build_transfer(alice.keypair, bob.address, 10)
    assert txid(tx) == txid(tx.model_copy(update={"signature": None}))
    assert txid(tx) != txid(tx.model_copy(update={"fee": DEVNET.min_fee + 1}))
    assert tx_size(
        tx.model_copy(update={"payload": {**tx.payload, "padding": "x" * 50}})
    ) > tx_size(tx)
