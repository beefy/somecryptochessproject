"""WalletService builders for the on-chain DEX.

The chain tests in ``test_dex.py`` exercise consensus directly. These tests
exercise the same transactions through the builder the CLI uses, so a signer and
a payload that the chain accepts are checked together.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from sccp.chain import dex
from sccp.chain.state import Pool, State
from sccp.chain.tx import apply_transaction, validate_transaction
from sccp.config import CHESS960_ASSET_ID, DEVNET
from sccp.errors import WalletServiceError
from sccp.protocol.models import TxType
from sccp.wallet.service import WalletService

COINS = 100_000_000
HEIGHT = 5


def _state(accounts: Mapping[str, Any]) -> State:
    state = State(params=DEVNET, height=HEIGHT - 1, timestamps=[1_700_000_000])
    for account in accounts.values():
        entry = state.ensure(account.address)
        entry.public_key = account.keypair.public_key_hex
        entry.balance = 1_000 * COINS
        entry.assets[CHESS960_ASSET_ID] = 100 * COINS
    return state


def _apply(state: State, tx: Any, height: int) -> None:
    validate_transaction(tx, DEVNET, state, height=height)
    apply_transaction(state, tx, height=height)


def test_create_pool_builds_the_canonical_pair_and_validates(
    protocol_accounts: Mapping[str, Any],
) -> None:
    state = _state(protocol_accounts)
    wallet = WalletService(DEVNET, state)
    alice = protocol_accounts["alice"]

    tx = wallet.build_create_pool(
        alice.keypair,
        asset_a=None,
        amount_a=10 * COINS,
        asset_b=CHESS960_ASSET_ID,
        amount_b=20 * COINS,
    )

    assert tx.type is TxType.CREATE_POOL
    assert "asset_a" not in tx.payload
    assert tx.payload["asset_b"] == CHESS960_ASSET_ID
    assert (tx.payload["amount_a"], tx.payload["amount_b"]) == (10 * COINS, 20 * COINS)
    _apply(state, tx, HEIGHT)

    pool_id = dex.pool_id(dex.NATIVE_ASSET, CHESS960_ASSET_ID)
    assert state.pools[pool_id].shares[alice.address] == dex.initial_shares(10 * COINS, 20 * COINS)


def test_add_swap_and_remove_build_the_same_state_transitions(
    protocol_accounts: Mapping[str, Any],
) -> None:
    state = _state(protocol_accounts)
    wallet = WalletService(DEVNET, state)
    alice = protocol_accounts["alice"]
    bob = protocol_accounts["bob"]
    pool_id = dex.pool_id(dex.NATIVE_ASSET, CHESS960_ASSET_ID)

    _apply(
        state,
        wallet.build_create_pool(
            alice.keypair,
            asset_a=None,
            amount_a=10 * COINS,
            asset_b=CHESS960_ASSET_ID,
            amount_b=20 * COINS,
        ),
        HEIGHT,
    )
    _apply(
        state,
        wallet.build_add_liquidity(
            alice.keypair,
            pool_id=pool_id,
            amount_a=COINS,
            amount_b=2 * COINS,
        ),
        HEIGHT + 1,
    )

    quote = dex.swap_output(11 * COINS, 22 * COINS, COINS, DEVNET.dex_fee_bps)
    _apply(
        state,
        wallet.build_swap(
            bob.keypair,
            pool_id=pool_id,
            asset_in=None,
            amount_in=COINS,
            min_amount_out=quote,
        ),
        HEIGHT + 2,
    )
    assert state.asset_balance(bob.address, CHESS960_ASSET_ID) == 100 * COINS + quote

    owned = state.pools[pool_id].shares[alice.address]
    _apply(
        state,
        wallet.build_remove_liquidity(alice.keypair, pool_id=pool_id, shares=owned),
        HEIGHT + 3,
    )
    assert pool_id not in state.pools


def test_the_builders_refuse_obvious_mistakes(protocol_accounts: Mapping[str, Any]) -> None:
    state = _state(protocol_accounts)
    wallet = WalletService(DEVNET, state)
    alice = protocol_accounts["alice"]
    pool_id = dex.pool_id(dex.NATIVE_ASSET, CHESS960_ASSET_ID)

    with pytest.raises(WalletServiceError, match="two different assets"):
        wallet.build_create_pool(
            alice.keypair,
            asset_a=None,
            amount_a=COINS,
            asset_b="ZQWX",
            amount_b=COINS,
        )
    with pytest.raises(WalletServiceError, match=r"does not hold|units of"):
        wallet.build_create_pool(
            alice.keypair,
            asset_a=None,
            amount_a=COINS,
            asset_b=CHESS960_ASSET_ID,
            amount_b=1_000 * COINS,
        )
    with pytest.raises(WalletServiceError, match="pool id"):
        wallet.build_add_liquidity(
            alice.keypair, pool_id="not-a-pool", amount_a=COINS, amount_b=COINS
        )
    with pytest.raises(WalletServiceError, match="negative"):
        wallet.build_swap(
            alice.keypair,
            pool_id=pool_id,
            asset_in=None,
            amount_in=COINS,
            min_amount_out=-1,
        )


def test_swap_builder_carries_token_input_and_guards_the_quote(
    protocol_accounts: Mapping[str, Any],
) -> None:
    state = _state(protocol_accounts)
    wallet = WalletService(DEVNET, state)
    alice = protocol_accounts["alice"]
    bob = protocol_accounts["bob"]
    carol = protocol_accounts["carol"]
    pool_id = dex.pool_id(dex.NATIVE_ASSET, CHESS960_ASSET_ID)
    _apply(
        state,
        wallet.build_create_pool(
            alice.keypair,
            asset_a=None,
            amount_a=10 * COINS,
            asset_b=CHESS960_ASSET_ID,
            amount_b=20 * COINS,
        ),
        HEIGHT,
    )

    quote = dex.swap_output(20 * COINS, 10 * COINS, COINS, DEVNET.dex_fee_bps)
    tx = wallet.build_swap(
        bob.keypair,
        pool_id=pool_id,
        asset_in=CHESS960_ASSET_ID,
        amount_in=COINS,
        min_amount_out=quote,
    )
    assert tx.payload["asset_in"] == CHESS960_ASSET_ID
    _apply(state, tx, HEIGHT + 1)

    with pytest.raises(WalletServiceError, match="positive"):
        wallet.build_swap(
            bob.keypair,
            pool_id=pool_id,
            asset_in=None,
            amount_in=0,
            min_amount_out=0,
        )
    with pytest.raises(WalletServiceError, match="unknown asset"):
        wallet.build_swap(
            bob.keypair,
            pool_id=pool_id,
            asset_in="DOGE",
            amount_in=1,
            min_amount_out=0,
        )
    with pytest.raises(WalletServiceError, match="below the requested minimum"):
        pool = state.pools[pool_id]
        too_much = dex.swap_output(pool.reserve_a, pool.reserve_b, COINS, DEVNET.dex_fee_bps) + 1
        wallet.build_swap(
            bob.keypair,
            pool_id=pool_id,
            asset_in=None,
            amount_in=COINS,
            min_amount_out=too_much,
        )
    with pytest.raises(WalletServiceError, match="lowercase"):
        wallet.build_swap(
            bob.keypair,
            pool_id=pool_id.upper(),
            asset_in=None,
            amount_in=1,
            min_amount_out=0,
        )
    with pytest.raises(WalletServiceError, match="hexadecimal"):
        wallet.build_swap(
            bob.keypair,
            pool_id="gg" * 32,
            asset_in=None,
            amount_in=1,
            min_amount_out=0,
        )
    with pytest.raises(WalletServiceError, match="spendable"):
        wallet.build_swap(
            bob.keypair,
            pool_id=pool_id,
            asset_in=None,
            amount_in=2_000 * COINS,
            min_amount_out=0,
        )

    state.ensure(carol.address).balance = 0
    with pytest.raises(WalletServiceError, match="cannot pay"):
        wallet.build_swap(
            carol.keypair,
            pool_id=pool_id,
            asset_in=CHESS960_ASSET_ID,
            amount_in=COINS,
            min_amount_out=0,
        )

    unknown = wallet.build_swap(
        bob.keypair,
        pool_id="ab" * 32,
        asset_in=None,
        amount_in=1,
        min_amount_out=0,
    )
    assert unknown.type is TxType.SWAP

    other_id = "cd" * 32
    state.pools[other_id] = Pool(
        other_id,
        "aa" * 32,
        "bb" * 32,
        10,
        20,
        14,
        {alice.address: 14},
    )
    with pytest.raises(WalletServiceError, match="does not trade"):
        wallet.build_swap(
            bob.keypair,
            pool_id=other_id,
            asset_in=None,
            amount_in=1,
            min_amount_out=0,
        )


def test_liquidity_and_create_pool_builders_refuse_bad_deposits(
    protocol_accounts: Mapping[str, Any],
) -> None:
    state = _state(protocol_accounts)
    wallet = WalletService(DEVNET, state)
    alice = protocol_accounts["alice"]
    bob = protocol_accounts["bob"]
    carol = protocol_accounts["carol"]
    pool_id = dex.pool_id(dex.NATIVE_ASSET, CHESS960_ASSET_ID)
    _apply(
        state,
        wallet.build_create_pool(
            alice.keypair,
            asset_a=None,
            amount_a=10 * COINS,
            asset_b=CHESS960_ASSET_ID,
            amount_b=20 * COINS,
        ),
        HEIGHT,
    )

    with pytest.raises(WalletServiceError, match="already exists"):
        wallet.build_create_pool(
            alice.keypair,
            asset_a=None,
            amount_a=10 * COINS,
            asset_b=CHESS960_ASSET_ID,
            amount_b=20 * COINS,
        )
    with pytest.raises(WalletServiceError, match="positive"):
        wallet.build_create_pool(
            alice.keypair,
            asset_a=None,
            amount_a=0,
            asset_b=CHESS960_ASSET_ID,
            amount_b=20 * COINS,
        )
    with pytest.raises(WalletServiceError, match="positive"):
        wallet.build_add_liquidity(
            alice.keypair,
            pool_id=pool_id,
            amount_a=0,
            amount_b=1,
        )
    with pytest.raises(WalletServiceError, match="too small"):
        wallet.build_add_liquidity(
            alice.keypair,
            pool_id=pool_id,
            amount_a=1,
            amount_b=1,
        )
    with pytest.raises(WalletServiceError, match="spendable"):
        wallet.build_add_liquidity(
            alice.keypair,
            pool_id=pool_id,
            amount_a=2_000 * COINS,
            amount_b=4_000 * COINS,
        )
    with pytest.raises(WalletServiceError, match="units of"):
        wallet.build_add_liquidity(
            alice.keypair,
            pool_id=pool_id,
            amount_a=COINS,
            amount_b=4_000 * COINS,
        )
    with pytest.raises(WalletServiceError, match="positive"):
        wallet.build_remove_liquidity(alice.keypair, pool_id=pool_id, shares=0)
    with pytest.raises(WalletServiceError, match="pool shares"):
        wallet.build_remove_liquidity(bob.keypair, pool_id=pool_id, shares=1)
    with pytest.raises(WalletServiceError, match="too small"):
        wallet.build_remove_liquidity(alice.keypair, pool_id=pool_id, shares=1)
    with pytest.raises(WalletServiceError, match="lowercase"):
        wallet.build_add_liquidity(
            alice.keypair,
            pool_id=pool_id.upper(),
            amount_a=COINS,
            amount_b=2 * COINS,
        )
    with pytest.raises(WalletServiceError, match="hexadecimal"):
        wallet.build_remove_liquidity(alice.keypair, pool_id="gg" * 32, shares=1)

    state.ensure(carol.address).balance = 0
    with pytest.raises(WalletServiceError, match="cannot pay"):
        wallet.build_add_liquidity(
            carol.keypair,
            pool_id=pool_id,
            amount_a=COINS,
            amount_b=2 * COINS,
        )

    unknown = "ab" * 32
    assert (
        wallet.build_add_liquidity(
            alice.keypair,
            pool_id=unknown,
            amount_a=COINS,
            amount_b=2 * COINS,
        ).type
        is TxType.ADD_LIQUIDITY
    )
    assert (
        wallet.build_remove_liquidity(
            alice.keypair,
            pool_id=unknown,
            shares=1,
        ).type
        is TxType.REMOVE_LIQUIDITY
    )

    empty = WalletService(
        DEVNET,
        State(params=DEVNET, height=HEIGHT - 1, timestamps=[1_700_000_000]),
    )
    with pytest.raises(WalletServiceError, match="cannot pay"):
        empty.build_add_liquidity(
            alice.keypair,
            pool_id=unknown,
            amount_a=1,
            amount_b=1,
        )
    with pytest.raises(WalletServiceError, match="cannot pay"):
        empty.build_remove_liquidity(alice.keypair, pool_id=unknown, shares=1)
