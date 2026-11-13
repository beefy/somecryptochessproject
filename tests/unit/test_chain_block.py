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
    # A bootstrap block carries no proofs, so it earns its seal work alone.
    assert connected.work == 1 << block.header.seal_bits
    assert connected.work < connected.state.difficulty.work
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


def test_connect_accepts_a_proofless_heartbeat_block(
    live_chain: Any, protocol_accounts: Any
) -> None:
    from sccp.chain.tx import make_coinbase
    from sccp.wallet.service import WalletService

    chain = live_chain.chain
    tx = WalletService(chain.params, chain.state).build_transfer(
        live_chain.faucet, protocol_accounts["alice"].address, 1
    )
    block = _propose(chain, protocol_accounts["carol"].keypair, txs=(tx,))
    assert block.proofs == ()
    coinbase = block.coinbase
    assert coinbase is not None
    assert coinbase.payload["amount"] == tx.fee  # fees only: a heartbeat mints nothing
    connected = connect_block(
        block,
        params=chain.params,
        parent_state=chain.state,
        parent_hash=chain.tip_hash,
        book=chain.book,
    )
    assert connected.state.height == chain.height + 1
    # ``minted`` counts every coinbase credit, so it grows by the fees only: the
    # heartbeat block adds no subsidy to the supply.
    assert connected.state.minted == chain.state.minted + tx.fee
    assert connected.work == 1 << block.header.seal_bits
    assert connected.work < connected.state.difficulty.work
    account = connected.state.account(block.header.miner)
    assert account.coinbase_queue[-1] == (block.header.height, tx.fee)

    stripped = block.model_copy(
        update={
            "transactions": (),
            "coinbase": make_coinbase(block.header.miner, 0, chain.params.network_id),
        }
    )
    stripped = _header(stripped, merkle_root=merkle_root_of(stripped))
    assert _reason(chain, stripped) is BlockFailure.PROOFS


def test_a_proofless_block_carries_exactly_one_transaction(
    live_chain: Any, protocol_accounts: Any
) -> None:
    """The one-transaction rule is consensus, not builder policy.

    A proofless block is the chain's clock, so a miner that folded the pool into
    one heartbeat could spend an epoch's ledger work without producing the
    blocks that work was meant to buy, and re-freeze the chain at the boundary.
    """
    from sccp.chain.mempool import Mempool
    from sccp.wallet.service import WalletService

    chain = live_chain.chain
    pool = Mempool(params=chain.params, tip_state=chain.state, book=chain.book)
    first = WalletService(chain.params, pool.projection).build_transfer(
        live_chain.faucet, protocol_accounts["alice"].address, 1
    )
    pool.add_transaction(first)
    second = WalletService(chain.params, pool.projection).build_transfer(
        live_chain.faucet, protocol_accounts["bob"].address, 1
    )

    one = _propose(chain, protocol_accounts["carol"].keypair, txs=(first,))
    assert one.proofs == ()
    assert len(one.transactions) == 1
    connected = connect_block(
        one,
        params=chain.params,
        parent_state=chain.state,
        parent_hash=chain.tip_hash,
        book=chain.book,
    )
    assert connected.state.height == chain.height + 1

    two = one.model_copy(update={"transactions": (first, second)})
    two = _header(two, merkle_root=merkle_root_of(two))
    assert _reason(chain, two) is BlockFailure.PROOFS


def test_a_proofless_block_earns_only_its_seal_work(
    live_chain: Any, chain_match: Any, miner_keys: dict[str, Any], protocol_accounts: Any
) -> None:
    """Equal-height blocks: the one carrying the proof must be the heavier."""
    from sccp.wallet.service import WalletService

    chain = live_chain.chain
    match = chain_match(chain, epoch=3)
    miner = miner_keys[match.proof.winner]
    proof_block = _propose(chain, miner, proofs=(match.proof,))
    tx = WalletService(chain.params, chain.state).build_transfer(
        live_chain.faucet, protocol_accounts["alice"].address, 1
    )
    heartbeat = _propose(chain, protocol_accounts["carol"].keypair, txs=(tx,))
    assert proof_block.header.height == heartbeat.header.height
    assert len(proof_block.proofs) == 1 and heartbeat.proofs == ()

    def connect(block: Any) -> Any:
        return connect_block(
            block,
            params=chain.params,
            parent_state=chain.state,
            parent_hash=chain.tip_hash,
            book=chain.book,
        )

    proof_connected = connect(proof_block)
    heartbeat_connected = connect(heartbeat)
    # Both declare the same difficulty, so they differ only in the chess term.
    declared = proof_connected.state.difficulty.work
    assert proof_connected.work == declared
    assert heartbeat_connected.work == declared - heartbeat.header.wins_required * 65536
    assert heartbeat_connected.work == 1 << heartbeat.header.seal_bits
    assert heartbeat_connected.work < proof_connected.work


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
