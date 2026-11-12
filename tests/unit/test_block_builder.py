"""The block builder: subsidy, fees, proofs and the seal (plan 5.10/5.11)."""

from __future__ import annotations

from typing import Any

from sccp.chain.block import block_hash, block_size, block_subsidy, seal_valid
from sccp.chain.chain import Chain
from sccp.chain.difficulty import Difficulty
from sccp.chain.genesis import build_genesis
from sccp.chain.mempool import Mempool
from sccp.config import DEVNET
from sccp.crypto.address import encode_address
from sccp.miner.block_builder import BlockBuilder
from sccp.wallet.service import WalletService

COINS = 100_000_000


def _builder(chain: Any, keypair: Any, pool: Mempool | None = None) -> BlockBuilder:
    return BlockBuilder(
        params=chain.params,
        chain=chain,
        mempool=pool or Mempool(params=chain.params, tip_state=chain.state, book=chain.book),
        keypair=keypair,
    )


def test_the_builder_reports_its_miner_and_difficulty(
    devnet_chain: Any, protocol_accounts: Any
) -> None:
    builder = _builder(devnet_chain, protocol_accounts["carol"].keypair)
    assert builder.miner == encode_address(
        protocol_accounts["carol"].keypair.public_key_bytes, DEVNET
    )
    assert builder.next_difficulty(1) == Difficulty(
        wins_required=DEVNET.wins_required, seal_bits=DEVNET.seal_bits
    )


def test_a_bootstrap_block_pays_the_subsidy_plus_the_fees(
    devnet_chain: Any, protocol_accounts: Any
) -> None:
    chain = devnet_chain
    miner = protocol_accounts["carol"].keypair
    faucet = WalletService(chain.params, chain.state)
    from sccp.chain.genesis import faucet_keypair

    tx = faucet.build_transfer(
        faucet_keypair("devnet"), protocol_accounts["alice"].address, 5 * COINS
    )
    pool = Mempool(params=chain.params, tip_state=chain.state, book=chain.book)
    pool.add_transaction(tx)
    block = _builder(chain, miner, pool).build(timestamp=chain.state.timestamps[-1] + 1)
    assert block is not None
    coinbase = block.coinbase
    assert coinbase is not None
    assert block.transactions == (tx,)
    assert coinbase.payload["amount"] == block_subsidy(chain.params, 1, chain.state.minted) + tx.fee
    assert coinbase.sender == block.header.miner
    assert chain.add_block(block) is not None
    assert chain.height == 1
    assert chain.state.minted == chain.genesis_state.minted + coinbase.payload["amount"]


def test_the_builder_refuses_to_invent_a_win(live_chain: Any, protocol_accounts: Any) -> None:
    chain = live_chain.chain
    assert (
        _builder(chain, protocol_accounts["carol"].keypair).build(
            timestamp=chain.state.timestamps[-1] + 1
        )
        is None
    )


def test_a_proof_block_records_the_win(
    live_chain: Any, chain_match: Any, miner_keys: dict[str, Any]
) -> None:
    chain = live_chain.chain
    match = chain_match(chain, epoch=3)
    pool = Mempool(params=chain.params, tip_state=chain.state, book=chain.book)
    pool.add_proof(match.proof)
    winner = match.proof.winner
    builder = _builder(chain, miner_keys[winner], pool)
    block = builder.build(timestamp=chain.state.timestamps[-1] + 1)
    assert block is not None
    assert [proof.proof_id for proof in block.proofs] == [match.proof.proof_id]
    assert block.header.miner == winner
    chain.add_block(block)
    state = chain.state
    assert match.proof.game_hash in state.proofs
    assert state.proofs[match.proof.game_hash].winner == winner
    assert state.matches[match.proof.match_id].epoch == 3
    stats = state.miner_stats[winner]
    assert stats.wins == 1
    assert stats.games == 1
    assert stats.last_opponent_epoch[match.proof.loser] == 3


def test_build_and_append_reports_a_refusal(live_chain: Any, protocol_accounts: Any) -> None:
    chain = live_chain.chain
    builder = _builder(chain, protocol_accounts["carol"].keypair)
    assert builder.build_and_append(timestamp=chain.state.timestamps[-1] + 1) is None
    assert chain.height == live_chain.chain.height


def test_the_builder_seals_against_the_declared_bits(protocol_accounts: Any) -> None:
    params = DEVNET.model_copy(update={"genesis_hash": None, "seal_bits": 8})
    genesis, state = build_genesis(params, timestamp=1_700_000_000)
    chain = Chain(params=params, genesis_block=genesis, genesis_state=state, book=None)
    builder = _builder(chain, protocol_accounts["carol"].keypair)
    block = builder.build(timestamp=1_700_000_001)
    assert block is not None
    assert block.header.seal_bits == 8
    assert seal_valid(block_hash(block.header), 8)
    assert block_size(block) > 0
