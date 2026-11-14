"""The node's optional proof-settling miner."""

from __future__ import annotations

from typing import Any

from sccp.chain.mempool import Mempool
from sccp.config import DEVNET
from sccp.node.miner import NodeMiner
from sccp.node.service import NodeService


def test_node_miner_mines_the_bootstrap_window_then_waits_for_proofs() -> None:
    service = NodeService.open(DEVNET)
    miner = NodeMiner(service, _address())
    for expected in (1, 2, 3):
        block = miner.mine_once()
        assert block is not None
        assert block.header.height == expected
        assert not block.proofs
    assert miner.mine_once() is None
    assert service.height == 3
    service.close()


def test_a_prebuilt_chain_disables_empty_bootstrap_mining() -> None:
    service = NodeService.open(DEVNET)
    miner = NodeMiner(service, _address(), mine_bootstrap=False)
    assert miner.mine_once() is None
    assert service.height == 0
    service.close()


def test_node_miner_settles_a_pending_proof(live_chain: Any, chain_match: Any) -> None:
    chain = live_chain.chain
    pool = Mempool(params=chain.params, tip_state=chain.state, book=chain.book)
    service = NodeService(params=chain.params, chain=chain, mempool=pool)
    match = chain_match(chain, epoch=3)
    service.announce_proof(match.proof)
    miner = NodeMiner(service, match.proof.winner)
    block = miner.mine_once()
    assert block is not None
    assert [proof.proof_id for proof in block.proofs] == [match.proof.proof_id]
    assert service.height == 4
    assert not service.mempool.proofs
    assert miner.mine_once() is None
    service.close()


def test_a_paced_miner_waits_for_the_tip_to_age(live_chain: Any, chain_match: Any) -> None:
    """Pacing keeps block timestamps at the target, so the retarget stays neutral."""
    chain = live_chain.chain
    pool = Mempool(params=chain.params, tip_state=chain.state, book=chain.book)
    service = NodeService(params=chain.params, chain=chain, mempool=pool)
    match = chain_match(chain, epoch=3)
    service.announce_proof(match.proof)
    now = [chain.state.timestamps[-1]]
    spacing = chain.params.target_block_seconds
    miner = NodeMiner(
        service,
        match.proof.winner,
        clock=lambda: now[0],
        min_interval_seconds=spacing,
    )
    # A block mined in the tip's own second would make the chain look too fast,
    # which the difficulty schedule answers by demanding more wins per block.
    assert miner.mine_once() is None
    now[0] += spacing - 1
    assert miner.mine_once() is None
    now[0] += 1
    block = miner.mine_once()
    assert block is not None
    assert block.header.height == 4
    assert block.header.timestamp == now[0]
    assert service.height == 4
    service.close()


def _address() -> str:
    from sccp.crypto.address import encode_address
    from sccp.crypto.pq import hybrid_keypair_from_seed

    return encode_address(hybrid_keypair_from_seed(bytes([5]) * 32).public_key_bytes, DEVNET)
