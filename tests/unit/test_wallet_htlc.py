"""WalletService builders for bridge HTLCs, variants and registrations."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

import pytest

from sccp.chain.state import HtlcRecord, State
from sccp.chain.tx import apply_transaction, validate_transaction
from sccp.config import CHESS960_ASSET_ID, DEVNET
from sccp.crypto.address import encode_address
from sccp.crypto.pq import hybrid_keypair_from_seed
from sccp.errors import WalletServiceError
from sccp.protocol.models import TxType
from sccp.wallet.service import WalletService

COINS = 100_000_000
HEIGHT = 5
START = 1_700_000_000
PREIMAGE = bytes.fromhex("11" * 32)
HASHLOCK = hashlib.sha256(PREIMAGE).hexdigest()


def _state(accounts: Mapping[str, Any]) -> State:
    state = State(params=DEVNET, height=HEIGHT - 1, timestamps=[START])
    for account in accounts.values():
        entry = state.ensure(account.address)
        entry.public_key = account.keypair.public_key_hex
        entry.balance = 1_000 * COINS
        entry.assets[CHESS960_ASSET_ID] = 100 * COINS
    return state


def _record(
    lock_id: str,
    sender: str,
    beneficiary: str,
    *,
    asset: str = "",
    amount: int = COINS,
) -> HtlcRecord:
    return HtlcRecord(
        htlc_id=lock_id,
        sender=sender,
        beneficiary=beneficiary,
        hashlock=HASHLOCK,
        timelock_height=HEIGHT + 10,
        amount=amount,
        created_at_height=HEIGHT,
        asset=asset,
    )


def test_htlc_lock_builds_native_and_token_locks(protocol_accounts: Mapping[str, Any]) -> None:
    state = _state(protocol_accounts)
    wallet = WalletService(DEVNET, state)
    alice = protocol_accounts["alice"]
    bob = protocol_accounts["bob"]

    native = wallet.build_htlc_lock(
        alice.keypair,
        hashlock=HASHLOCK,
        timelock_height=HEIGHT + 10,
        beneficiary=bob.address,
        amount=COINS,
        nonce=0,
    )
    assert native.type is TxType.HTLC_LOCK
    assert native.payload["amount"] == COINS
    assert native.payload["beneficiary"] == bob.address
    assert "asset" not in native.payload
    validate_transaction(native, DEVNET, state, height=HEIGHT)

    token = wallet.build_htlc_lock(
        alice.keypair,
        hashlock=HASHLOCK,
        timelock_height=HEIGHT + 10,
        beneficiary=bob.address,
        amount=COINS,
        asset="C960",
    )
    assert token.payload["asset"] == CHESS960_ASSET_ID
    validate_transaction(token, DEVNET, state, height=HEIGHT)


def test_htlc_lock_guards_cover_every_precondition(
    protocol_accounts: Mapping[str, Any],
) -> None:
    state = _state(protocol_accounts)
    wallet = WalletService(DEVNET, state)
    alice = protocol_accounts["alice"]
    bob = protocol_accounts["bob"]

    def build(**updates: object) -> Any:
        values: dict[str, object] = {
            "hashlock": HASHLOCK,
            "timelock_height": HEIGHT + 10,
            "beneficiary": bob.address,
            "amount": COINS,
        }
        values.update(updates)
        return wallet.build_htlc_lock(alice.keypair, **values)

    with pytest.raises(WalletServiceError, match="not a valid"):
        build(beneficiary="sccpt1nope")
    with pytest.raises(WalletServiceError, match="positive"):
        build(amount=0)
    with pytest.raises(WalletServiceError, match="lowercase"):
        build(hashlock=HASHLOCK.upper())
    with pytest.raises(WalletServiceError, match="hexadecimal"):
        build(hashlock="gg" * 32)
    with pytest.raises(WalletServiceError, match="not after"):
        build(timelock_height=state.height)
    with pytest.raises(WalletServiceError, match="unknown asset"):
        build(asset="DOGE")
    with pytest.raises(WalletServiceError, match="cannot lock"):
        build(amount=2_000 * COINS)

    stranger = hybrid_keypair_from_seed(bytes([9]) * 32)
    with pytest.raises(WalletServiceError, match="cannot lock"):
        wallet.build_htlc_lock(
            stranger,
            hashlock=HASHLOCK,
            timelock_height=HEIGHT + 10,
            beneficiary=bob.address,
            amount=1,
        )

    stranger_address = encode_address(stranger.public_key_bytes, DEVNET)
    state.ensure(stranger_address).assets[CHESS960_ASSET_ID] = 1
    with pytest.raises(WalletServiceError, match="cannot pay"):
        wallet.build_htlc_lock(
            stranger,
            hashlock=HASHLOCK,
            timelock_height=HEIGHT + 10,
            beneficiary=bob.address,
            amount=1,
            asset=CHESS960_ASSET_ID,
        )


def test_htlc_claim_reads_local_and_node_supplied_records(
    protocol_accounts: Mapping[str, Any],
) -> None:
    state = _state(protocol_accounts)
    wallet = WalletService(DEVNET, state)
    alice = protocol_accounts["alice"]
    bob = protocol_accounts["bob"]
    lock_id = "ab" * 32
    record = _record(lock_id, alice.address, bob.address)
    state.htlcs[lock_id] = record

    claim = wallet.build_htlc_claim(
        bob.keypair,
        htlc_id=lock_id,
        preimage=PREIMAGE.hex(),
        nonce=0,
    )
    assert claim.type is TxType.HTLC_CLAIM
    assert claim.payload["preimage"] == PREIMAGE.hex()
    validate_transaction(claim, DEVNET, state, height=HEIGHT)
    apply_transaction(state, claim, height=HEIGHT)
    assert state.htlcs[lock_id].preimage == PREIMAGE.hex()

    token_id = "cd" * 32
    token_record = _record(token_id, alice.address, bob.address, asset=CHESS960_ASSET_ID)
    supplied = wallet.build_htlc_claim(
        bob.keypair,
        htlc_id=token_id,
        preimage=PREIMAGE.hex(),
        record=token_record,
    )
    assert supplied.type is TxType.HTLC_CLAIM
    assert token_id not in state.htlcs


def test_htlc_claim_guards_cover_every_precondition(
    protocol_accounts: Mapping[str, Any],
) -> None:
    state = _state(protocol_accounts)
    wallet = WalletService(DEVNET, state)
    alice = protocol_accounts["alice"]
    bob = protocol_accounts["bob"]
    lock_id = "ab" * 32
    record = _record(lock_id, alice.address, bob.address)
    state.htlcs[lock_id] = record

    with pytest.raises(WalletServiceError, match="unknown htlc"):
        wallet.build_htlc_claim(
            bob.keypair,
            htlc_id="ef" * 32,
            preimage=PREIMAGE.hex(),
        )
    with pytest.raises(WalletServiceError, match="different lock"):
        wallet.build_htlc_claim(
            bob.keypair,
            htlc_id=lock_id,
            preimage=PREIMAGE.hex(),
            record=_record("ef" * 32, alice.address, bob.address),
        )
    with pytest.raises(WalletServiceError, match="pays"):
        wallet.build_htlc_claim(
            alice.keypair,
            htlc_id=lock_id,
            preimage=PREIMAGE.hex(),
        )
    with pytest.raises(WalletServiceError, match="lowercase"):
        wallet.build_htlc_claim(
            bob.keypair,
            htlc_id=lock_id,
            preimage="AA" * 32,
        )
    with pytest.raises(WalletServiceError, match="hexadecimal"):
        wallet.build_htlc_claim(
            bob.keypair,
            htlc_id=lock_id,
            preimage="gg" * 32,
        )
    with pytest.raises(WalletServiceError, match="does not open"):
        wallet.build_htlc_claim(
            bob.keypair,
            htlc_id=lock_id,
            preimage="22" * 32,
        )

    state.ensure(bob.address).balance = 0
    state.htlcs[lock_id] = _record(lock_id, alice.address, bob.address, amount=1)
    with pytest.raises(WalletServiceError, match="cannot cover"):
        wallet.build_htlc_claim(
            bob.keypair,
            htlc_id=lock_id,
            preimage=PREIMAGE.hex(),
        )
    token_record = _record("cd" * 32, alice.address, bob.address, asset=CHESS960_ASSET_ID)
    with pytest.raises(WalletServiceError, match="cannot pay"):
        wallet.build_htlc_claim(
            bob.keypair,
            htlc_id="cd" * 32,
            preimage=PREIMAGE.hex(),
            record=token_record,
        )


def test_htlc_refund_builds_from_local_and_node_supplied_records(
    protocol_accounts: Mapping[str, Any],
) -> None:
    state = _state(protocol_accounts)
    wallet = WalletService(DEVNET, state)
    alice = protocol_accounts["alice"]
    bob = protocol_accounts["bob"]
    lock_id = "ab" * 32
    record = _record(lock_id, alice.address, bob.address)
    state.htlcs[lock_id] = record

    refund = wallet.build_htlc_refund(
        alice.keypair,
        htlc_id=lock_id,
        nonce=0,
    )
    assert refund.type is TxType.HTLC_REFUND
    assert refund.payload["htlc_id"] == lock_id
    validate_transaction(refund, DEVNET, state, height=HEIGHT + 10)

    token_id = "cd" * 32
    token_record = _record(token_id, alice.address, bob.address, asset=CHESS960_ASSET_ID)
    supplied = wallet.build_htlc_refund(
        alice.keypair,
        htlc_id=token_id,
        record=token_record,
    )
    assert supplied.type is TxType.HTLC_REFUND
    assert token_id not in state.htlcs


def test_htlc_refund_guards_cover_every_precondition(
    protocol_accounts: Mapping[str, Any],
) -> None:
    state = _state(protocol_accounts)
    wallet = WalletService(DEVNET, state)
    alice = protocol_accounts["alice"]
    bob = protocol_accounts["bob"]
    lock_id = "ab" * 32
    record = _record(lock_id, alice.address, bob.address)
    state.htlcs[lock_id] = record

    with pytest.raises(WalletServiceError, match="unknown htlc"):
        wallet.build_htlc_refund(alice.keypair, htlc_id="ef" * 32)
    with pytest.raises(WalletServiceError, match="different lock"):
        wallet.build_htlc_refund(
            alice.keypair,
            htlc_id=lock_id,
            record=_record("ef" * 32, alice.address, bob.address),
        )
    with pytest.raises(WalletServiceError, match="only"):
        wallet.build_htlc_refund(bob.keypair, htlc_id=lock_id)

    state.ensure(alice.address).balance = 0
    state.htlcs[lock_id] = _record(lock_id, alice.address, bob.address, amount=1)
    with pytest.raises(WalletServiceError, match="cannot cover"):
        wallet.build_htlc_refund(alice.keypair, htlc_id=lock_id)
    token_record = _record("cd" * 32, alice.address, bob.address, asset=CHESS960_ASSET_ID)
    with pytest.raises(WalletServiceError, match="cannot pay"):
        wallet.build_htlc_refund(
            alice.keypair,
            htlc_id="cd" * 32,
            record=token_record,
        )


def test_register_builder_handles_variants_and_an_unfunded_sender(
    protocol_accounts: Mapping[str, Any],
) -> None:
    state = _state(protocol_accounts)
    wallet = WalletService(DEVNET, state)
    alice = protocol_accounts["alice"]

    tx = wallet.build_register(
        alice.keypair,
        endpoint="tcp://alice.test",
        engine="stockfish",
        variant="chess960",
    )
    assert tx.payload["variant"] == "chess960"
    with pytest.raises(WalletServiceError, match="unknown variant"):
        wallet.build_register(
            alice.keypair,
            endpoint="tcp://alice.test",
            engine="stockfish",
            variant="bogus",
        )

    stranger = hybrid_keypair_from_seed(bytes([9]) * 32)
    with pytest.raises(WalletServiceError, match="cannot cover"):
        wallet.build_register(stranger, endpoint="tcp://a", engine="stockfish")


def test_transfer_builder_carries_a_variant_asset(
    protocol_accounts: Mapping[str, Any],
) -> None:
    state = _state(protocol_accounts)
    wallet = WalletService(DEVNET, state)
    alice = protocol_accounts["alice"]
    bob = protocol_accounts["bob"]

    tx = wallet.build_transfer(alice.keypair, bob.address, COINS, asset="C960", nonce=0)
    assert tx.payload["asset"] == CHESS960_ASSET_ID
    validate_transaction(tx, DEVNET, state, height=HEIGHT)

    with pytest.raises(WalletServiceError, match="unknown asset"):
        wallet.build_transfer(alice.keypair, bob.address, COINS, asset="DOGE")
    with pytest.raises(WalletServiceError, match="the transfer needs"):
        wallet.build_transfer(alice.keypair, bob.address, 200 * COINS, asset="C960")

    state.ensure(alice.address).balance = 0
    with pytest.raises(WalletServiceError, match="cannot pay"):
        wallet.build_transfer(alice.keypair, bob.address, COINS, asset="C960")
