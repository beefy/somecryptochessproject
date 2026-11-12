"""Blocks: header commitments, subsidy, bootstrap and the connect checklist."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from sccp.chain.block import (
    HEADER_VERSION,
    BlockFailure,
    block_hash,
    block_size,
    block_subsidy,
    body_root,
    bootstrap_end_height,
    connect_block,
    header_payload,
    merkle_root_of,
    required_proofs,
)
from sccp.chain.genesis import load_genesis
from sccp.chain.mempool import Mempool
from sccp.chain.state import ZERO_HASH
from sccp.config import DEVNET, MAINNET, NetworkParams
from sccp.errors import BlockValidationError
from sccp.miner.block_builder import BlockBuilder

GENESIS_DIR = Path(__file__).resolve().parents[2] / "genesis"

HEADER_KEYS = {
    "version",
    "network",
    "height",
    "prev_hash",
    "merkle_root",
    "state_root",
    "roster_root",
    "timestamp",
    "epoch",
    "beacon_hash",
    "wins_required",
    "seal_bits",
    "seal_nonce",
    "miner",
}


def _propose(
    chain: Any,
    keypair: Any,
    *,
    txs: tuple[Any, ...] = (),
    proofs: tuple[Any, ...] = (),
    timestamp: int | None = None,
) -> Any:
    """Build the next block without connecting it."""
    stamp = chain.state.timestamps[-1] + 1 if timestamp is None else timestamp
    pool = Mempool(params=chain.params, tip_state=chain.state, book=chain.book)
    for tx in txs:
        pool.add_transaction(tx)
    for proof in proofs:
        pool.add_proof(proof)
    block = BlockBuilder(params=chain.params, chain=chain, mempool=pool, keypair=keypair).build(
        timestamp=stamp
    )
    assert block is not None
    return block


def _reason(chain: Any, block: Any, **kwargs: Any) -> BlockFailure:
    with pytest.raises(BlockValidationError) as excinfo:
        connect_block(
            block,
            params=kwargs.pop("params", chain.params),
            parent_state=kwargs.pop("parent_state", chain.state),
            parent_hash=kwargs.pop("parent_hash", chain.tip_hash),
            book=kwargs.pop("book", chain.book),
            **kwargs,
        )
    return excinfo.value.reason


def _header(block: Any, **updates: Any) -> Any:
    return block.model_copy(update={"header": block.header.model_copy(update=updates)})


def test_genesis_header_commits_to_the_documented_fields(
    protocol_params: NetworkParams,
) -> None:
    block, state = load_genesis(GENESIS_DIR / "devnet.json", protocol_params)
    payload = header_payload(block.header)
    assert set(payload) == HEADER_KEYS
    assert block.header.version == HEADER_VERSION
    assert block.header.height == 0
    assert block.header.prev_hash == ZERO_HASH
    assert block.header.wins_required == protocol_params.wins_required
    assert block.header.seal_bits == protocol_params.seal_bits
    assert state.tip_hash == block_hash(block.header) == protocol_params.genesis_hash
    assert block.coinbase is None
    assert block.transactions == ()
    assert block.proofs == ()


def test_genesis_hash_is_pinned_per_network() -> None:
    for name, params in (("devnet", DEVNET), ("mainnet", MAINNET)):
        block, _ = load_genesis(GENESIS_DIR / f"{name}.json", params)
        assert block_hash(block.header) == params.genesis_hash


def test_body_root_is_order_sensitive_and_empty_by_default() -> None:
    assert body_root(()) == ZERO_HASH
    assert body_root(["aa", "bb"]) == body_root(["aa", "bb"])
    assert body_root(["aa", "bb"]) != body_root(["bb", "aa"])
    assert body_root(["aa"]) != ZERO_HASH


def test_merkle_root_follows_coinbase_transactions_proofs(
    devnet_chain: Any, protocol_accounts: Any
) -> None:
    from sccp.chain.tx import txid

    block = _propose(devnet_chain, protocol_accounts["carol"].keypair)
    assert block.coinbase is not None
    assert merkle_root_of(block) == body_root((txid(block.coinbase),))
    moved = block.model_copy(update={"coinbase": None})
    assert merkle_root_of(moved) == ZERO_HASH


def test_block_size_grows_with_the_body(devnet_chain: Any, protocol_accounts: Any) -> None:
    block = _propose(devnet_chain, protocol_accounts["carol"].keypair)
    assert block_size(block) > 0
    assert block_size(block.model_copy(update={"coinbase": None})) < block_size(block)


def test_block_subsidy_halves_and_respects_the_supply_cap() -> None:
    interval = MAINNET.halving_interval
    assert interval is not None
    assert block_subsidy(DEVNET, 10_000, 0) == DEVNET.subsidy
    assert block_subsidy(MAINNET, 0, 0) == MAINNET.subsidy
    assert block_subsidy(MAINNET, interval, 0) == MAINNET.subsidy // 2
    assert block_subsidy(MAINNET, 2 * interval, 0) == MAINNET.subsidy // 4
    cap = MAINNET.max_money
    assert cap is not None
    assert block_subsidy(MAINNET, 0, cap - 1) == 1
    assert block_subsidy(MAINNET, 0, cap) == 0


def test_bootstrap_window_matches_the_epoch_schedule() -> None:
    assert bootstrap_end_height(DEVNET) == 3
    assert bootstrap_end_height(MAINNET) == 79
    for height in range(bootstrap_end_height(DEVNET) + 1):
        assert required_proofs(DEVNET, height, 1) == 0
    assert required_proofs(DEVNET, bootstrap_end_height(DEVNET) + 1, 1) == 1
    assert required_proofs(MAINNET, 100, 3) == 3
    assert required_proofs(MAINNET, 80, 3) == 3


def test_connect_accepts_a_valid_bootstrap_block(devnet_chain: Any, protocol_accounts: Any) -> None:
    miner = protocol_accounts["carol"].keypair
    block = _propose(devnet_chain, miner)
    connected = connect_block(
        block,
        params=devnet_chain.params,
        parent_state=devnet_chain.state,
        parent_hash=devnet_chain.tip_hash,
        book=devnet_chain.book,
    )
    coinbase = block.coinbase
    assert coinbase is not None
    assert connected.block_hash == block_hash(block.header)
    assert connected.state.height == 1
    assert connected.state.tip_hash == connected.block_hash
    assert connected.work == connected.state.difficulty.work
    value = coinbase.payload["amount"]
    assert isinstance(value, int)
    assert value == block_subsidy(devnet_chain.params, 1, 0)
    assert connected.state.account(block.header.miner).immature == value
    assert connected.state.minted == devnet_chain.state.minted + value
    assert connected.state.timestamps[-1] == block.header.timestamp


def test_connect_rejects_a_broken_header(devnet_chain: Any, protocol_accounts: Any) -> None:
    miner = protocol_accounts["carol"].keypair
    block = _propose(devnet_chain, miner)
    assert _reason(devnet_chain, _header(block, prev_hash="ab" * 32)) is BlockFailure.PREV_HASH
    assert _reason(devnet_chain, _header(block, height=7)) is BlockFailure.HEIGHT
    assert _reason(devnet_chain, _header(block, network="testnet")) is BlockFailure.NETWORK
    assert _reason(devnet_chain, _header(block, version=2)) is BlockFailure.SCHEMA
    assert _reason(devnet_chain, _header(block, epoch=5)) is BlockFailure.EPOCH
    assert _reason(devnet_chain, _header(block, wins_required=2)) is BlockFailure.DIFFICULTY


def test_connect_rejects_tampered_commitments(devnet_chain: Any, protocol_accounts: Any) -> None:
    miner = protocol_accounts["carol"].keypair
    block = _propose(devnet_chain, miner)
    assert _reason(devnet_chain, _header(block, merkle_root="ab" * 32)) is BlockFailure.MERKLE
    assert _reason(devnet_chain, _header(block, state_root="ab" * 32)) is BlockFailure.STATE_ROOT

    coinbase = block.coinbase
    assert coinbase is not None
    rich = coinbase.model_copy(update={"payload": {**coinbase.payload, "amount": 1}})
    overpaid = block.model_copy(update={"coinbase": rich})
    overpaid = _header(overpaid, merkle_root=merkle_root_of(overpaid))
    assert _reason(devnet_chain, overpaid) is BlockFailure.COINBASE
    assert (
        _reason(devnet_chain, block.model_copy(update={"coinbase": None})) is BlockFailure.COINBASE
    )


def test_connect_enforces_the_timestamp_window(devnet_chain: Any, protocol_accounts: Any) -> None:
    miner = protocol_accounts["carol"].keypair
    parent_time = devnet_chain.state.timestamps[-1]
    stale = _propose(devnet_chain, miner, timestamp=parent_time)
    assert _reason(devnet_chain, stale) is BlockFailure.TIMESTAMP
    future = _propose(devnet_chain, miner, timestamp=parent_time + 60 * 60 * 3)
    assert _reason(devnet_chain, future, now=parent_time) is BlockFailure.TIMESTAMP
    relaxed = connect_block(
        future,
        params=devnet_chain.params,
        parent_state=devnet_chain.state,
        parent_hash=devnet_chain.tip_hash,
        book=devnet_chain.book,
    )
    assert relaxed.state.height == 1


def test_connect_requires_the_scheduled_proofs(
    live_chain: Any, chain_match: Any, miner_keys: dict[str, Any]
) -> None:
    chain = live_chain.chain
    match = chain_match(chain, epoch=3)
    miner = miner_keys[match.proof.winner]
    good = _propose(chain, miner, proofs=(match.proof,))
    assert len(good.proofs) == 1
    stripped = good.model_copy(update={"proofs": ()})
    assert _reason(chain, stripped) is BlockFailure.PROOFS


def test_connect_rejects_a_duplicated_transaction(
    live_chain: Any, chain_match: Any, miner_keys: dict[str, Any], protocol_accounts: Any
) -> None:
    from sccp.wallet.service import WalletService

    chain = live_chain.chain
    alice = protocol_accounts["alice"]
    wallet = WalletService(chain.params, chain.state)
    tx = wallet.build_transfer(alice.keypair, protocol_accounts["bob"].address, 1, fee=2_000)
    match = chain_match(chain, epoch=3)
    block = _propose(chain, miner_keys[match.proof.winner], txs=(tx,), proofs=(match.proof,))
    doubled = block.model_copy(update={"transactions": (*block.transactions, tx)})
    doubled = _header(doubled, merkle_root=merkle_root_of(doubled))
    assert _reason(chain, doubled) is BlockFailure.DUPLICATE
