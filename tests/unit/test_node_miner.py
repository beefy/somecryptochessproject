"""The node's optional proof-settling miner (plan M6)."""

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
    service.close()


def _address() -> str:
    from sccp.crypto.address import encode_address
    from sccp.crypto.keys import keypair_from_seed

    return encode_address(keypair_from_seed(bytes([5]) * 32).public_key_bytes, DEVNET)
