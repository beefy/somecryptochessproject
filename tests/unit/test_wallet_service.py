"""Wallet service: balances, builders and history (PROTOCOL.md)."""

from __future__ import annotations

from typing import Any

import pytest

from sccp.chain.tx import validate_transaction
from sccp.config import DEVNET, format_coins
from sccp.crypto.address import encode_address
from sccp.errors import WalletServiceError
from sccp.protocol.models import TxType
from sccp.wallet.service import Balance, WalletService

COINS = 100_000_000


def test_the_service_reports_the_network_and_addresses(live_chain: Any) -> None:
    chain = live_chain.chain
    wallet = WalletService(DEVNET, chain.state, chain=chain)
    alice = live_chain.accounts["alice"]
    assert wallet.network == "devnet"
    assert wallet.address_for(alice.keypair) == alice.address
    assert wallet.next_nonce(alice.address) == 3
    stranger = encode_address(b"\x09" * 32, DEVNET)
    assert wallet.account(stranger).balance == 0
    with pytest.raises(WalletServiceError, match="not a valid"):
        wallet.account("sccpt1qunknown")


def test_balances_split_spendable_immature_and_bonded(live_chain: Any) -> None:
    chain = live_chain.chain
    wallet = WalletService(DEVNET, chain.state, chain=chain)
    alice = live_chain.accounts["alice"].address
    balance = wallet.balance(alice)
    assert isinstance(balance, Balance)
    assert balance.address == alice
    assert balance.total == balance.spendable + balance.immature + balance.bonded
    assert balance.spendable == 100 * COINS - 3 * DEVNET.min_fee
    assert balance.immature == 0
    assert balance.bonded == 0


def test_the_minimum_fee_is_enforced(live_chain: Any) -> None:
    wallet = WalletService(DEVNET, live_chain.chain.state)
    assert wallet.required_fee(None) == DEVNET.min_fee
    assert wallet.required_fee(DEVNET.min_fee * 2) == DEVNET.min_fee * 2
    with pytest.raises(WalletServiceError, match="below the network minimum"):
        wallet.required_fee(DEVNET.min_fee - 1)


def test_a_transfer_is_signed_and_valid(live_chain: Any) -> None:
    chain = live_chain.chain
    wallet = WalletService(DEVNET, chain.state)
    alice = live_chain.accounts["alice"]
    bob = live_chain.accounts["bob"]
    tx = wallet.build_transfer(alice.keypair, bob.address, COINS)
    assert tx.type is TxType.TRANSFER
    assert tx.sender == alice.address
    assert tx.payload["amount"] == COINS
    assert tx.nonce == 3
    assert tx.signature is not None
    validate_transaction(tx, DEVNET, chain.state, height=chain.height + 1)


def test_transfer_guards(live_chain: Any) -> None:
    chain = live_chain.chain
    wallet = WalletService(DEVNET, chain.state)
    alice = live_chain.accounts["alice"]
    bob = live_chain.accounts["bob"]
    with pytest.raises(WalletServiceError, match="positive"):
        wallet.build_transfer(alice.keypair, bob.address, 0)
    with pytest.raises(WalletServiceError, match="different recipient"):
        wallet.build_transfer(alice.keypair, alice.address, COINS)
    with pytest.raises(WalletServiceError, match="spendable"):
        wallet.build_transfer(alice.keypair, bob.address, 10**15)
    with pytest.raises(WalletServiceError, match="not a valid"):
        wallet.build_transfer(alice.keypair, "sccpt1nope", COINS)
    with pytest.raises(WalletServiceError, match="nonce"):
        wallet.build_transfer(alice.keypair, bob.address, COINS, nonce=9)


def test_a_refused_transfer_names_coins_and_the_local_chain(live_chain: Any) -> None:
    """Base units in a refusal look like coins; print coins and say what to check."""
    chain = live_chain.chain
    wallet = WalletService(DEVNET, chain.state)
    alice = live_chain.accounts["alice"]
    bob = live_chain.accounts["bob"]
    spendable = chain.state.spendable(alice.address)
    with pytest.raises(WalletServiceError) as caught:
        wallet.build_transfer(alice.keypair, bob.address, 100 * COINS)
    message = str(caught.value)
    assert f"has {format_coins(spendable)} spendable" in message
    assert (
        f"needs {format_coins(100 * COINS + DEVNET.min_fee)} "
        f"({format_coins(100 * COINS)} plus a {format_coins(DEVNET.min_fee)} fee)"
    ) in message
    assert "--data-dir" in message


def test_register_respects_the_minimum_bond(live_chain: Any) -> None:
    chain = live_chain.chain
    params = DEVNET.model_copy(update={"min_bond": 1_000})
    wallet = WalletService(params, chain.state)
    alice = live_chain.accounts["alice"]
    tx = wallet.build_register(alice.keypair, endpoint="tcp://a", engine="mock-v1")
    assert tx.payload["bond"] == 1_000
    assert tx.type is TxType.REGISTER
    with pytest.raises(WalletServiceError, match="below the minimum"):
        wallet.build_register(alice.keypair, endpoint="tcp://a", engine="mock-v1", bond=999)


def test_availability_and_reveal_target_the_next_block_epoch(live_chain: Any) -> None:
    chain = live_chain.chain
    wallet = WalletService(DEVNET, chain.state)
    alice = live_chain.accounts["alice"]
    availability = wallet.build_availability(alice.keypair, nonce_secret="33" * 32, max_games=1)
    expected = chain.height + 1 + DEVNET.availability_lead
    assert availability.payload["epoch"] == expected
    with pytest.raises(WalletServiceError, match="must be committed"):
        wallet.build_availability(alice.keypair, nonce_secret="33" * 32, epoch=expected + 1)
    reveal = wallet.build_reveal(alice.keypair, "33" * 32)
    assert reveal.type is TxType.REVEAL
    assert reveal.payload["epoch"] == chain.height + 2


def test_history_walks_the_adopted_chain(live_chain: Any) -> None:
    chain = live_chain.chain
    wallet = WalletService(DEVNET, chain.state, chain=chain)
    faucet_entries = wallet.history(live_chain.faucet_address)
    assert len(faucet_entries) == 2
    assert {entry.type for entry in faucet_entries} == {TxType.TRANSFER}
    assert all(entry.delta == -(100 * COINS + DEVNET.min_fee) for entry in faucet_entries)
    assert {entry.counterparty for entry in faucet_entries} == {
        live_chain.accounts["alice"].address,
        live_chain.accounts["bob"].address,
    }
    assert all(entry.height == 1 for entry in faucet_entries)

    alice = live_chain.accounts["alice"].address
    entries = wallet.history(alice)
    assert [entry.type for entry in entries] == [
        TxType.TRANSFER,
        TxType.REGISTER,
        TxType.AVAILABILITY,
        TxType.REVEAL,
    ]
    assert entries[0].delta == 100 * COINS
    assert entries[0].counterparty == live_chain.faucet_address
    assert entries[1].delta == -DEVNET.min_fee
    assert entries[1].counterparty is None
    assert [entry.height for entry in entries] == [1, 1, 1, 2]


def test_history_needs_a_chain(live_chain: Any) -> None:
    wallet = WalletService(DEVNET, live_chain.chain.state)
    with pytest.raises(WalletServiceError, match="needs a chain"):
        wallet.history(live_chain.accounts["alice"].address)


def test_history_rejects_a_foreign_address(live_chain: Any) -> None:
    wallet = WalletService(DEVNET, live_chain.chain.state, chain=live_chain.chain)
    with pytest.raises(WalletServiceError, match="not a valid"):
        wallet.history("mainnet1nope")
