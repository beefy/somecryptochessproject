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
from sccp.chain.state import State
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
            asset_b="SCCP",
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
