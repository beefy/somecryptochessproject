"""Block tree, fork choice and reorganisation (plan section 5.11)."""

from __future__ import annotations

from typing import Any

import pytest

from sccp.chain.chain import Chain, Reorg, new_chain_from_document
from sccp.chain.genesis import block_hash, genesis_document
from sccp.chain.mempool import Mempool
from sccp.config import DEVNET
from sccp.errors import ChainError
from sccp.miner.block_builder import BlockBuilder


def _mine(chain: Chain, keypair: Any, *, offset: int = 0) -> Any:
    """Mine one block on ``chain`` with a deterministic timestamp."""
    stamp = chain.state.timestamps[-1] + 1 + offset
    pool = Mempool(params=chain.params, tip_state=chain.state, book=chain.book)
    block = BlockBuilder(params=chain.params, chain=chain, mempool=pool, keypair=keypair).build(
        timestamp=stamp
    )
    assert block is not None
    return block


def _replay(chain: Chain, blocks: list[Any]) -> Chain:
    """A second chain object holding the same prefix, to mine a side branch."""
    fork = Chain(
        params=chain.params,
        genesis_block=chain.genesis_block,
        genesis_state=chain.genesis_state,
        book=chain.book,
    )
    for block in blocks:
        fork.add_block(block)
    return fork


def test_a_new_chain_holds_only_its_genesis(devnet_chain: Chain) -> None:
    chain = devnet_chain
    assert chain.height == 0
    assert chain.tip_hash == DEVNET.genesis_hash
    assert chain.genesis_hash == DEVNET.genesis_hash
    assert chain.tip.block_hash == chain.tip_hash
    assert chain.has(chain.tip_hash) is True
    assert chain.get("00" * 32) is None
    assert [stored.height for stored in chain.main_chain()] == [0]
    assert chain.block_at_height(0) is chain.tip
    assert chain.block_at_height(1) is None
    assert chain.block_at_height(-1) is None
    assert chain.is_main_chain(chain.tip_hash) is True


def test_chain_rejects_a_bogus_genesis(devnet_chain: Chain) -> None:
    bad_state = devnet_chain.state.copy()
    bad_state.height = 1
    with pytest.raises(ChainError, match="height 0"):
        Chain(
            params=devnet_chain.params,
            genesis_block=devnet_chain.genesis_block,
            genesis_state=bad_state,
        )


def test_adding_the_same_block_twice_is_a_no_op(
    devnet_chain: Chain, protocol_accounts: Any
) -> None:
    chain = devnet_chain
    block = _mine(chain, protocol_accounts["carol"].keypair)
    assert isinstance(chain.add_block(block), Reorg)
    assert chain.height == 1
    assert chain.add_block(block) is None
    assert chain.height == 1
    assert len(chain.blocks) == 2


def test_an_orphan_waits_for_its_parent(devnet_chain: Chain, protocol_accounts: Any) -> None:
    chain = devnet_chain
    miner = protocol_accounts["carol"].keypair
    first = _mine(chain, miner)
    side = _replay(chain, [first])
    second = _mine(side, miner)
    assert chain.add_block(second) is None
    assert chain.height == 0
    assert chain.pending_orphans() == 1
    chain.add_block(first)
    assert chain.height == 2
    assert chain.pending_orphans() == 0
    assert chain.tip.block_hash == block_hash(second.header)


def test_fork_choice_breaks_ties_on_the_lower_hash(
    devnet_chain: Chain, protocol_accounts: Any
) -> None:
    chain = devnet_chain
    miner = protocol_accounts["carol"].keypair
    left = _mine(chain, miner)
    chain.add_block(left)
    right = _mine(_replay(chain, []), miner, offset=10)
    assert right.header.timestamp != left.header.timestamp
    chain.add_block(right)
    winner = min((left, right), key=lambda block: block_hash(block.header))
    assert chain.tip_hash == block_hash(winner.header)


def test_a_heavier_branch_reorganises_the_tip(devnet_chain: Chain, protocol_accounts: Any) -> None:
    from sccp.chain.genesis import faucet_keypair
    from sccp.wallet.service import WalletService

    chain = devnet_chain
    miner = protocol_accounts["carol"].keypair
    faucet = faucet_keypair("devnet")
    tx = WalletService(chain.params, chain.state).build_transfer(
        faucet, protocol_accounts["alice"].address, 100 * 100_000_000
    )
    pool = Mempool(params=chain.params, tip_state=chain.state, book=chain.book)
    pool.add_transaction(tx)
    stamp = chain.state.timestamps[-1] + 1
    left = BlockBuilder(params=chain.params, chain=chain, mempool=pool, keypair=miner).build(
        timestamp=stamp
    )
    assert left is not None
    chain.add_block(left)

    side = _replay(chain, [])
    adopted: list[Any] = []
    for offset in (1, 2):
        block = _mine(side, miner, offset=offset)
        side.add_block(block)
        adopted.append(block)
    assert chain.add_block(adopted[0]) is None
    reorg = chain.add_block(adopted[1])
    assert isinstance(reorg, Reorg)
    assert reorg.old_tip == block_hash(left.header)
    assert reorg.new_tip == chain.tip_hash == block_hash(adopted[1].header)
    assert [block_hash(block.header) for block in reorg.orphaned] == [block_hash(left.header)]
    assert [block_hash(block.header) for block in reorg.adopted] == [
        block_hash(block.header) for block in adopted
    ]
    assert reorg.orphaned_transactions == (tx,)
    assert reorg.orphaned_proofs == ()
    assert chain.height == 2
    assert chain.is_main_chain(block_hash(left.header)) is False
    assert [stored.height for stored in chain.main_chain()] == [0, 1, 2]


def test_add_blocks_reports_every_reorg(devnet_chain: Chain, protocol_accounts: Any) -> None:
    chain = devnet_chain
    miner = protocol_accounts["carol"].keypair
    side = _replay(chain, [])
    blocks = []
    for offset in (1, 2):
        block = _mine(side, miner, offset=offset)
        side.add_block(block)
        blocks.append(block)
    reorgs = chain.add_blocks(blocks)
    assert reorgs
    assert reorgs[-1].new_tip == chain.tip_hash == block_hash(blocks[-1].header)
    assert chain.height == 2


def test_new_chain_from_document_and_file() -> None:
    from pathlib import Path

    params = DEVNET.model_copy(update={"genesis_hash": None})
    document = genesis_document(params, timestamp=1_700_000_000)
    chain = new_chain_from_document(params, document)
    assert chain.height == 0
    assert chain.params is params
    assert chain.tip_hash == document["hash"]

    path = Path(__file__).resolve().parents[2] / "genesis" / "devnet.json"
    loaded = Chain.from_genesis_file("devnet", str(path), params=DEVNET.model_copy(update={}))
    assert loaded.params.genesis_hash == DEVNET.genesis_hash
    assert loaded.tip_hash == DEVNET.genesis_hash
