"""The DEX: constant-product pools as a consensus state transition."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from sccp.chain.dex import (
    NATIVE_ASSET,
    added_shares,
    initial_shares,
    normalize_pair,
    proportional_amount,
    removed_amounts,
    swap_output,
)
from sccp.chain.dex import (
    pool_id as derive_pool_id,
)
from sccp.chain.genesis import faucet_keypair
from sccp.chain.state import Pool, State
from sccp.chain.tx import (
    TxFailure,
    apply_transaction,
    sign_transaction,
    validate_transaction,
)
from sccp.config import CHESS960_ASSET_ID, DEVNET
from sccp.crypto.address import encode_address
from sccp.errors import StateError, TxValidationError
from sccp.protocol.models import Transaction, TxType

COINS = 100_000_000
HEIGHT = 5
START = 1_700_000_000


def _state(accounts: Mapping[str, Any]) -> State:
    state = State(params=DEVNET, height=HEIGHT - 1, timestamps=[START])
    for account in accounts.values():
        entry = state.ensure(account.address)
        entry.public_key = account.keypair.public_key_hex
        entry.balance = 1_000 * COINS
        entry.assets[CHESS960_ASSET_ID] = 100 * COINS
    return state


def _tx(keypair: Any, kind: TxType, nonce: int, payload: dict[str, object]) -> Transaction:
    unsigned = Transaction(
        network=DEVNET.network_id,
        type=kind,
        sender=encode_address(keypair.public_key_bytes, DEVNET),
        nonce=nonce,
        fee=DEVNET.min_fee,
        payload={"public_key": keypair.public_key_hex, **payload},
    )
    return sign_transaction(keypair, unsigned)


def _apply(state: State, tx: Transaction) -> None:
    validate_transaction(tx, DEVNET, state, height=HEIGHT)
    apply_transaction(state, tx, height=HEIGHT)


def _reason(state: State, tx: Transaction) -> TxFailure:
    with pytest.raises(TxValidationError) as excinfo:
        validate_transaction(tx, DEVNET, state, height=HEIGHT)
    return excinfo.value.reason


def _create(
    keypair: Any,
    native_amount: int,
    token_amount: int,
    *,
    nonce: int = 0,
    asset: str = CHESS960_ASSET_ID,
) -> Transaction:
    return _tx(
        keypair,
        TxType.CREATE_POOL,
        nonce,
        {
            "asset_b": asset,
            "amount_a": native_amount,
            "amount_b": token_amount,
        },
    )


def test_create_pool_mints_shares_and_holds_both_reserves(
    protocol_accounts: Mapping[str, Any],
) -> None:
    state = _state(protocol_accounts)
    alice = protocol_accounts["alice"]
    before_native = state.account(alice.address).balance
    before_token = state.asset_balance(alice.address, CHESS960_ASSET_ID)

    native_amount = 10 * COINS
    token_amount = 20 * COINS
    tx = _create(alice.keypair, native_amount, token_amount)
    _apply(state, tx)

    expected_id = derive_pool_id(NATIVE_ASSET, CHESS960_ASSET_ID)
    pool = state.pools[expected_id]
    expected_shares = initial_shares(native_amount, token_amount)
    assert pool.asset_a == NATIVE_ASSET
    assert pool.asset_b == CHESS960_ASSET_ID
    assert (pool.reserve_a, pool.reserve_b) == (native_amount, token_amount)
    assert pool.total_shares == expected_shares
    assert pool.shares == {alice.address: expected_shares}
    assert state.account(alice.address).balance == before_native - native_amount - DEVNET.min_fee
    assert state.asset_balance(alice.address, CHESS960_ASSET_ID) == before_token - token_amount


def test_add_liquidity_is_proportional_to_the_pool(
    protocol_accounts: Mapping[str, Any],
) -> None:
    state = _state(protocol_accounts)
    alice = protocol_accounts["alice"]
    native_amount = 10 * COINS
    token_amount = 20 * COINS
    _apply(state, _create(alice.keypair, native_amount, token_amount))
    pool_id = derive_pool_id(NATIVE_ASSET, CHESS960_ASSET_ID)
    pool = state.pools[pool_id]
    before = pool.total_shares

    extra_native = 5 * COINS
    extra_token = 10 * COINS
    tx = _tx(
        alice.keypair,
        TxType.ADD_LIQUIDITY,
        1,
        {"pool_id": pool_id, "amount_a": extra_native, "amount_b": extra_token},
    )
    _apply(state, tx)

    expected = added_shares(native_amount, token_amount, before, extra_native, extra_token)
    assert expected > 0
    assert pool.reserve_a == native_amount + extra_native
    assert pool.reserve_b == token_amount + extra_token
    assert pool.total_shares == before + expected
    assert pool.shares[alice.address] == before + expected


def test_swap_moves_both_reserves_and_honours_the_minimum(
    protocol_accounts: Mapping[str, Any],
) -> None:
    state = _state(protocol_accounts)
    alice = protocol_accounts["alice"]
    bob = protocol_accounts["bob"]
    _apply(state, _create(alice.keypair, 10 * COINS, 20 * COINS))
    pool_id = derive_pool_id(NATIVE_ASSET, CHESS960_ASSET_ID)
    pool = state.pools[pool_id]
    amount_in = COINS
    expected_out = swap_output(10 * COINS, 20 * COINS, amount_in, DEVNET.dex_fee_bps)
    assert expected_out > 0

    tx = _tx(
        bob.keypair,
        TxType.SWAP,
        0,
        {
            "pool_id": pool_id,
            "amount_in": amount_in,
            "min_amount_out": expected_out,
        },
    )
    _apply(state, tx)

    assert pool.reserve_a == 11 * COINS
    assert pool.reserve_b == 20 * COINS - expected_out
    assert state.asset_balance(bob.address, CHESS960_ASSET_ID) == 100 * COINS + expected_out
    assert state.account(bob.address).balance == 1_000 * COINS - amount_in - DEVNET.min_fee

    greedy = _tx(
        bob.keypair,
        TxType.SWAP,
        1,
        {
            "pool_id": pool_id,
            "amount_in": amount_in,
            "min_amount_out": expected_out + 1,
        },
    )
    assert _reason(state, greedy) is TxFailure.TYPE


def test_remove_all_liquidity_returns_the_reserves_and_deletes_the_pool(
    protocol_accounts: Mapping[str, Any],
) -> None:
    state = _state(protocol_accounts)
    alice = protocol_accounts["alice"]
    _apply(state, _create(alice.keypair, 10 * COINS, 20 * COINS))
    pool_id = derive_pool_id(NATIVE_ASSET, CHESS960_ASSET_ID)
    pool = state.pools[pool_id]
    shares = pool.shares[alice.address]
    before_native = state.account(alice.address).balance
    before_token = state.asset_balance(alice.address, CHESS960_ASSET_ID)

    tx = _tx(
        alice.keypair,
        TxType.REMOVE_LIQUIDITY,
        1,
        {"pool_id": pool_id, "shares": shares},
    )
    _apply(state, tx)

    assert pool_id not in state.pools
    assert state.account(alice.address).balance == before_native + 10 * COINS - DEVNET.min_fee
    assert state.asset_balance(alice.address, CHESS960_ASSET_ID) == before_token + 20 * COINS


def test_remove_liquidity_rejects_more_than_the_sender_owns(
    protocol_accounts: Mapping[str, Any],
) -> None:
    state = _state(protocol_accounts)
    alice = protocol_accounts["alice"]
    bob = protocol_accounts["bob"]
    _apply(state, _create(alice.keypair, 10 * COINS, 20 * COINS))
    pool_id = derive_pool_id(NATIVE_ASSET, CHESS960_ASSET_ID)
    tx = _tx(
        bob.keypair,
        TxType.REMOVE_LIQUIDITY,
        0,
        {"pool_id": pool_id, "shares": 1},
    )
    assert _reason(state, tx) is TxFailure.BALANCE


def test_pools_are_committed_by_state_root_and_snapshots(
    protocol_accounts: Mapping[str, Any],
) -> None:
    state = _state(protocol_accounts)
    alice = protocol_accounts["alice"]
    empty_root = state.state_root()
    _apply(state, _create(alice.keypair, 10 * COINS, 20 * COINS))
    assert state.state_root() != empty_root

    restored = State.from_snapshot(DEVNET, state.snapshot())
    pool_id = derive_pool_id(NATIVE_ASSET, CHESS960_ASSET_ID)
    assert restored.pools == state.pools
    assert restored.pools[pool_id].shares == state.pools[pool_id].shares
    assert restored.state_root() == state.state_root()


def test_pool_rejects_an_unordered_or_unbalanced_book() -> None:
    pool_id = derive_pool_id(NATIVE_ASSET, CHESS960_ASSET_ID)
    with pytest.raises(StateError):
        Pool(
            pool_id,
            CHESS960_ASSET_ID,
            NATIVE_ASSET,
            1,
            1,
            1,
            {"sccpt1alice": 1},
        )
    with pytest.raises(StateError):
        Pool(pool_id, NATIVE_ASSET, CHESS960_ASSET_ID, 1, 1, 2, {"sccpt1alice": 1})


def test_duplicate_pool_and_unknown_asset_are_refused(
    protocol_accounts: Mapping[str, Any],
) -> None:
    state = _state(protocol_accounts)
    alice = protocol_accounts["alice"]
    _apply(state, _create(alice.keypair, 10 * COINS, 20 * COINS))
    duplicate = _create(alice.keypair, 10 * COINS, 20 * COINS, nonce=1)
    assert _reason(state, duplicate) is TxFailure.DUPLICATE

    unknown = _create(alice.keypair, 10 * COINS, 20 * COINS, nonce=1, asset="ab" * 32)
    assert _reason(state, unknown) is TxFailure.TYPE


def test_a_create_pool_transaction_survives_a_real_block(
    devnet_chain: Any, mine_block: Any
) -> None:
    """The builder, block validation and state root all see the new pool."""
    faucet = faucet_keypair("devnet")
    address = encode_address(faucet.public_key_bytes, DEVNET)
    devnet_chain.state.ensure(address).assets[CHESS960_ASSET_ID] = 20 * COINS
    tx = _create(faucet, 10 * COINS, 20 * COINS)

    block = mine_block(devnet_chain, faucet, txs=(tx,))

    assert len(block.transactions) == 1
    assert devnet_chain.height == 1
    pool_id = derive_pool_id(NATIVE_ASSET, CHESS960_ASSET_ID)
    assert pool_id in devnet_chain.state.pools
    assert devnet_chain.state.pools[pool_id].reserve_b == 20 * COINS


def test_the_pure_dex_helpers_cover_zero_and_invalid_inputs() -> None:
    assert normalize_pair("b", 2, "a", 1) == ("a", 1, "b", 2)
    assert normalize_pair("a", 1, "b", 2) == ("a", 1, "b", 2)
    with pytest.raises(StateError, match="two different assets"):
        normalize_pair("a", 1, "a", 2)
    with pytest.raises(StateError, match="two different assets"):
        derive_pool_id("a", "a")

    assert initial_shares(0, 5) == 0
    assert added_shares(0, 1, 1, 1, 1) == 0
    assert added_shares(1, 0, 1, 1, 1) == 0
    assert added_shares(0, 0, 1, 1, 1) == 0
    assert added_shares(1, 1, 0, 1, 1) == 0
    assert added_shares(10, 10, 10, 2, 2) == 2
    assert removed_amounts(10, 20, 10, 2) == (2, 4)
    assert removed_amounts(10, 20, 10, 0) == (0, 0)
    assert removed_amounts(10, 20, 0, 1) == (0, 0)

    assert swap_output(0, 1, 1, 30) == 0
    assert swap_output(1, 0, 1, 30) == 0
    assert swap_output(1, 1, 0, 30) == 0
    with pytest.raises(StateError, match="outside"):
        swap_output(1, 1, 1, 10_000)
    assert proportional_amount(0, 1, 1) == 0
    assert proportional_amount(1, 0, 1) == 0
    assert proportional_amount(3, 10, 4) == 2
