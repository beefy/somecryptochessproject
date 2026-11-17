"""The ETH<->SCCP bridge's SCCP leg and per-environment Ethereum settings."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

import pytest

from sccp.bridge.config import (
    ETH_CONTRACT_ENV,
    ETH_RPC_ENV,
    bridge_environment,
    eth_environments,
)
from sccp.chain.state import HtlcRecord, HtlcStatus, State
from sccp.chain.tx import (
    TxFailure,
    apply_transaction,
    htlc_id,
    sign_transaction,
    validate_transaction,
)
from sccp.config import CHESS960_ASSET_ID, DEVNET, TESTNET
from sccp.crypto.address import encode_address
from sccp.crypto.pq import hybrid_keypair_from_seed
from sccp.errors import TxValidationError
from sccp.node.app import create_app
from sccp.node.service import NodeService
from sccp.protocol.models import Transaction, TxType
from sccp.transport import client_for

COINS = 100_000_000
START = 1_700_000_000
PREIMAGE = bytes.fromhex("11" * 32)
HASHLOCK = hashlib.sha256(PREIMAGE).hexdigest()
CONTRACT = "0x" + "ab" * 20


def _state(accounts: Mapping[str, Any], *, bob_balance: int = 1_000 * COINS) -> State:
    state = State(params=DEVNET, height=10, timestamps=[START])
    for name, account in accounts.items():
        entry = state.ensure(account.address)
        entry.public_key = account.keypair.public_key_hex
        entry.balance = bob_balance if name == "bob" else 1_000 * COINS
        entry.assets[CHESS960_ASSET_ID] = 100 * COINS
    return state


def _tx(
    keypair: Any,
    kind: TxType,
    nonce: int,
    payload: dict[str, object],
    *,
    network: Any = DEVNET,
) -> Transaction:
    unsigned = Transaction(
        network=network.network_id,
        type=kind,
        sender=encode_address(keypair.public_key_bytes, network),
        nonce=nonce,
        fee=network.min_fee,
        payload={"public_key": keypair.public_key_hex, **payload},
    )
    return sign_transaction(keypair, unsigned)


def _apply(state: State, tx: Transaction, height: int = 11) -> None:
    validate_transaction(tx, DEVNET, state, height=height)
    apply_transaction(state, tx, height=height)


def _reason(state: State, tx: Transaction, height: int = 11) -> TxFailure:
    with pytest.raises(TxValidationError) as excinfo:
        validate_transaction(tx, DEVNET, state, height=height)
    return excinfo.value.reason


def _lock(
    keypair: Any,
    beneficiary: str,
    *,
    nonce: int = 0,
    amount: int = 10 * COINS,
    timelock: int = 20,
    hashlock: str = HASHLOCK,
    asset: str | None = None,
    fee: int = DEVNET.min_fee,
) -> Transaction:
    payload: dict[str, object] = {
        "hashlock": hashlock,
        "timelock_height": timelock,
        "beneficiary": beneficiary,
        "amount": amount,
    }
    if asset is not None:
        payload["asset"] = asset
    unsigned = Transaction(
        network=DEVNET.network_id,
        type=TxType.HTLC_LOCK,
        sender=encode_address(keypair.public_key_bytes, DEVNET),
        nonce=nonce,
        fee=fee,
        payload={"public_key": keypair.public_key_hex, **payload},
    )
    return sign_transaction(keypair, unsigned)


def test_lock_then_claim_pays_the_preimage_holder_from_escrow(
    protocol_accounts: Mapping[str, Any],
) -> None:
    state = _state(protocol_accounts, bob_balance=0)
    alice = protocol_accounts["alice"]
    bob = protocol_accounts["bob"]
    before = state.account(alice.address).balance

    _apply(state, _lock(alice.keypair, bob.address), height=10)
    lock_id = htlc_id(alice.address, 0)
    record = state.htlcs[lock_id]
    assert record.status is HtlcStatus.LOCKED
    assert record.amount == 10 * COINS
    assert record.hashlock == HASHLOCK
    assert state.account(alice.address).balance == before - 10 * COINS - DEVNET.min_fee

    claim = _tx(
        bob.keypair,
        TxType.HTLC_CLAIM,
        0,
        {"htlc_id": lock_id, "preimage": PREIMAGE.hex()},
    )
    _apply(state, claim)
    assert state.htlcs[lock_id].status is HtlcStatus.CLAIMED
    assert state.htlcs[lock_id].closed_at_height == 11
    assert state.account(bob.address).balance == 10 * COINS - DEVNET.min_fee
    assert state.account(bob.address).nonce == 1

    again = _tx(
        bob.keypair,
        TxType.HTLC_CLAIM,
        1,
        {"htlc_id": lock_id, "preimage": PREIMAGE.hex()},
    )
    assert _reason(state, again) is TxFailure.WINDOW


def test_a_wrong_preimage_is_rejected(protocol_accounts: Mapping[str, Any]) -> None:
    state = _state(protocol_accounts)
    alice = protocol_accounts["alice"]
    bob = protocol_accounts["bob"]
    lock_id = htlc_id(alice.address, 0)
    _apply(state, _lock(alice.keypair, bob.address), height=10)
    wrong = _tx(
        bob.keypair,
        TxType.HTLC_CLAIM,
        0,
        {"htlc_id": lock_id, "preimage": ("22" * 32)},
    )
    assert _reason(state, wrong) is TxFailure.TYPE


def test_claim_is_impossible_once_the_timelock_is_reached(
    protocol_accounts: Mapping[str, Any],
) -> None:
    state = _state(protocol_accounts)
    alice = protocol_accounts["alice"]
    bob = protocol_accounts["bob"]
    _apply(state, _lock(alice.keypair, bob.address, timelock=20), height=10)
    claim = _tx(
        bob.keypair,
        TxType.HTLC_CLAIM,
        0,
        {"htlc_id": htlc_id(alice.address, 0), "preimage": PREIMAGE.hex()},
    )
    assert _reason(state, claim, height=20) is TxFailure.WINDOW


def test_refund_returns_the_escrow_only_after_the_timelock(
    protocol_accounts: Mapping[str, Any],
) -> None:
    state = _state(protocol_accounts)
    alice = protocol_accounts["alice"]
    bob = protocol_accounts["bob"]
    before = state.account(alice.address).balance
    _apply(state, _lock(alice.keypair, bob.address, timelock=20), height=10)
    refund = _tx(alice.keypair, TxType.HTLC_REFUND, 1, {"htlc_id": htlc_id(alice.address, 0)})
    assert _reason(state, refund, height=19) is TxFailure.WINDOW

    _apply(state, refund, height=20)
    assert state.htlcs[htlc_id(alice.address, 0)].status is HtlcStatus.REFUNDED
    assert state.account(alice.address).balance == before - 2 * DEVNET.min_fee


def test_refund_is_signed_by_the_locker_not_the_beneficiary(
    protocol_accounts: Mapping[str, Any],
) -> None:
    state = _state(protocol_accounts)
    alice = protocol_accounts["alice"]
    bob = protocol_accounts["bob"]
    _apply(state, _lock(alice.keypair, bob.address, timelock=20), height=10)
    refund = _tx(bob.keypair, TxType.HTLC_REFUND, 0, {"htlc_id": htlc_id(alice.address, 0)})
    assert _reason(state, refund, height=20) is TxFailure.SIGNATURE


def test_a_variant_token_can_be_escrowed_too(protocol_accounts: Mapping[str, Any]) -> None:
    state = _state(protocol_accounts)
    alice = protocol_accounts["alice"]
    bob = protocol_accounts["bob"]
    before = state.account(alice.address).assets[CHESS960_ASSET_ID]
    _apply(
        state,
        _lock(alice.keypair, bob.address, asset=CHESS960_ASSET_ID, amount=7 * COINS),
        height=10,
    )
    lock_id = htlc_id(alice.address, 0)
    assert state.htlcs[lock_id].asset == CHESS960_ASSET_ID
    assert state.account(alice.address).assets[CHESS960_ASSET_ID] == before - 7 * COINS
    claim = _tx(
        bob.keypair,
        TxType.HTLC_CLAIM,
        0,
        {"htlc_id": lock_id, "preimage": PREIMAGE.hex()},
    )
    _apply(state, claim)
    assert state.account(bob.address).assets[CHESS960_ASSET_ID] == 100 * COINS + 7 * COINS


def test_lock_rejects_a_stale_timelock_and_an_unfunded_amount(
    protocol_accounts: Mapping[str, Any],
) -> None:
    state = _state(protocol_accounts)
    alice = protocol_accounts["alice"]
    bob = protocol_accounts["bob"]
    assert _reason(state, _lock(alice.keypair, bob.address, timelock=10), height=10) is (
        TxFailure.WINDOW
    )
    assert (
        _reason(state, _lock(alice.keypair, bob.address, amount=10_000 * COINS), height=10)
        is TxFailure.BALANCE
    )


def test_htlcs_are_committed_and_survive_a_snapshot(
    protocol_accounts: Mapping[str, Any],
) -> None:
    state = _state(protocol_accounts)
    alice = protocol_accounts["alice"]
    bob = protocol_accounts["bob"]
    empty_root = state.state_root()
    lock_id = htlc_id(alice.address, 0)
    _apply(state, _lock(alice.keypair, bob.address), height=10)
    assert state.state_root() != empty_root

    restored = State.from_snapshot(DEVNET, state.snapshot())
    assert restored.htlcs == state.htlcs
    assert restored.state_root() == state.state_root()

    _apply(
        state,
        _tx(
            bob.keypair,
            TxType.HTLC_CLAIM,
            0,
            {"htlc_id": lock_id, "preimage": PREIMAGE.hex()},
        ),
    )
    restored = State.from_snapshot(DEVNET, state.snapshot())
    assert restored.htlcs[lock_id].status is HtlcStatus.CLAIMED


def test_each_sccp_network_maps_to_exactly_one_ethereum_chain() -> None:
    environments = {environment.network: environment for environment in eth_environments({})}
    assert environments["devnet"].chain_id == 31337
    assert environments["testnet"].chain_id == 11155111
    assert environments["mainnet"].chain_id == 1
    assert environments["devnet"].chain_name == "Anvil devnet"
    assert environments["testnet"].chain_name == "Sepolia"
    assert environments["mainnet"].chain_name == "Ethereum"
    assert environments["mainnet"].sccp_margin == 20
    assert environments["testnet"].sccp_margin == 24
    assert environments["devnet"].sccp_margin == 24
    assert environments["devnet"].rpc_url == "http://127.0.0.1:8545"


def test_bridge_environment_reads_only_the_operator_overrides() -> None:
    environment = bridge_environment(
        TESTNET,
        {
            ETH_RPC_ENV: "https://sepolia.example/rpc",
            ETH_CONTRACT_ENV: CONTRACT.upper().replace("0X", "0x"),
            "SCCP_ETH_CONFIRMATIONS": "9",
            "SCCP_ETH_SCCP_MARGIN": "17",
        },
    )
    assert environment.chain_id == 11155111
    assert environment.rpc_url == "https://sepolia.example/rpc"
    assert environment.contract == CONTRACT
    assert environment.confirmations == 9
    assert environment.sccp_margin == 17
    assert environment.configured is True
    assert environment.as_dict()["eth_htlc_contract"] == CONTRACT


@pytest.mark.parametrize(
    ("contract", "message"),
    [
        ("0x1234", "20-byte address"),
        ("ab" * 20, "20-byte address"),
    ],
)
def test_a_malformed_contract_address_is_refused(contract: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        bridge_environment(DEVNET, {ETH_CONTRACT_ENV: contract})


def test_zero_confirmations_or_margin_are_refused() -> None:
    with pytest.raises(ValueError, match="SCCP_ETH_CONFIRMATIONS"):
        bridge_environment(DEVNET, {"SCCP_ETH_CONFIRMATIONS": "0"})
    with pytest.raises(ValueError, match="SCCP_ETH_SCCP_MARGIN"):
        bridge_environment(DEVNET, {"SCCP_ETH_SCCP_MARGIN": "0"})
    with pytest.raises(ValueError, match="reorg depth"):
        bridge_environment(DEVNET, {"SCCP_ETH_SCCP_MARGIN": "5"})


def test_htlc_routes_and_the_account_view_report_a_locked_record() -> None:
    sender_key = hybrid_keypair_from_seed(bytes([7]) * 32)
    beneficiary_key = hybrid_keypair_from_seed(bytes([8]) * 32)
    sender = encode_address(sender_key.public_key_bytes, DEVNET)
    beneficiary = encode_address(beneficiary_key.public_key_bytes, DEVNET)
    lock_id = "ab" * 32
    service = NodeService.open(DEVNET)
    try:
        service.chain.state.htlcs[lock_id] = HtlcRecord(
            htlc_id=lock_id,
            sender=sender,
            beneficiary=beneficiary,
            hashlock=HASHLOCK,
            timelock_height=20,
            amount=10 * COINS,
            created_at_height=10,
        )
        with client_for(create_app(service)) as client:
            view = client.get(f"/v1/htlcs/{lock_id}").json()
            account = client.get(f"/v1/accounts/{beneficiary}").json()
            by_hash = client.get(f"/v1/htlcs?hashlock={HASHLOCK}").json()
        assert view["status"] == "locked"
        assert view["asset"] is None
        assert view["preimage"] is None
        assert [row["htlc_id"] for row in by_hash["htlcs"]] == [lock_id]
        assert [row["htlc_id"] for row in account["htlcs"]] == [lock_id]
    finally:
        service.close()
