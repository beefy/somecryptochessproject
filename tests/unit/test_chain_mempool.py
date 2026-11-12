"""The mempool: admission, ordering, maintenance and proof pooling."""

from __future__ import annotations

from typing import Any

import pytest

from sccp.chain.genesis import faucet_keypair
from sccp.chain.mempool import Mempool
from sccp.chain.state import epoch_end_height
from sccp.chain.tx import txid
from sccp.crypto.address import encode_address
from sccp.errors import MempoolError
from sccp.wallet.service import WalletService

COINS = 100_000_000


def _pool(chain: Any) -> Mempool:
    return Mempool(params=chain.params, tip_state=chain.state, book=chain.book)


def _transfer(chain: Any, to: str, amount: int = 10 * COINS, fee: int | None = None) -> Any:
    faucet = faucet_keypair("devnet")
    return WalletService(chain.params, chain.state).build_transfer(faucet, to, amount, fee=fee)


def test_admission_updates_the_projection(devnet_chain: Any, protocol_accounts: Any) -> None:
    chain = devnet_chain
    alice = protocol_accounts["alice"].address
    pool = _pool(chain)
    assert pool.next_height == 1
    tx = _transfer(chain, alice)
    assert pool.add_transaction(tx) == txid(tx)
    assert len(pool) == 1
    assert pool.has_transaction(txid(tx)) is True
    assert pool.projection.account(alice).balance == 10 * COINS
    assert chain.state.account(alice).balance == 0


def test_the_pool_rejects_a_duplicate_txid(devnet_chain: Any, protocol_accounts: Any) -> None:
    chain = devnet_chain
    tx = _transfer(chain, protocol_accounts["alice"].address)
    pool = _pool(chain)
    pool.add_transaction(tx)
    with pytest.raises(MempoolError, match="already in the mempool"):
        pool.add_transaction(tx)


def test_the_pool_rejects_a_second_transaction_for_one_nonce(
    devnet_chain: Any, protocol_accounts: Any
) -> None:
    chain = devnet_chain
    pool = _pool(chain)
    pool.add_transaction(_transfer(chain, protocol_accounts["alice"].address))
    clash = _transfer(chain, protocol_accounts["bob"].address)
    with pytest.raises(MempoolError, match="nonce 0"):
        pool.add_transaction(clash)


def test_a_chained_nonce_is_admitted_against_the_projection(
    devnet_chain: Any, protocol_accounts: Any
) -> None:
    chain = devnet_chain
    alice = protocol_accounts["alice"].address
    pool = _pool(chain)
    first = _transfer(chain, alice, 1 * COINS)
    pool.add_transaction(first)
    faucet = faucet_keypair("devnet")
    second = WalletService(chain.params, pool.projection).build_transfer(
        faucet, protocol_accounts["bob"].address, 1 * COINS
    )
    pool.add_transaction(second)
    assert len(pool) == 2
    faucet_address = encode_address(faucet.public_key_bytes, chain.params)
    assert pool.projection.account(faucet_address).nonce == 2


def test_the_pool_rejects_an_invalid_transaction(devnet_chain: Any, protocol_accounts: Any) -> None:
    from sccp.chain.tx import sign_transaction

    chain = devnet_chain
    faucet = faucet_keypair("devnet")
    tx = _transfer(chain, protocol_accounts["alice"].address)
    greedy = tx.model_copy(update={"payload": {**tx.payload, "amount": 10**15}})
    with pytest.raises(MempoolError, match="balance"):
        _pool(chain).add_transaction(
            sign_transaction(faucet, greedy.model_copy(update={"signature": None}))
        )


def test_selection_orders_senders_by_fee_and_keeps_nonce_order(
    live_chain: Any, protocol_accounts: Any
) -> None:
    chain = live_chain.chain
    alice = protocol_accounts["alice"]
    bob = protocol_accounts["bob"]
    pool = _pool(chain)
    wallet = WalletService(chain.params, chain.state)
    pool.add_transaction(wallet.build_transfer(alice.keypair, bob.address, COINS, fee=1_000))
    pool.add_transaction(
        WalletService(chain.params, pool.projection).build_transfer(
            alice.keypair, bob.address, COINS, fee=5_000
        )
    )
    pool.add_transaction(
        WalletService(chain.params, pool.projection).build_transfer(
            bob.keypair, alice.address, COINS, fee=3_000
        )
    )
    chosen = pool.select_transactions(height=chain.height + 1)
    assert [(tx.sender, tx.nonce) for tx in chosen] == [
        (bob.address, 3),
        (alice.address, 3),
        (alice.address, 4),
    ]
    assert pool.select_transactions(height=chain.height + 1) == chosen
    assert pool.select_transactions(height=chain.height + 1, limit=2) == chosen[:2]
    assert pool.select_transactions(height=chain.height + 1, limit=1) == chosen[:1]


def test_selection_respects_the_size_cap(devnet_chain: Any, protocol_accounts: Any) -> None:
    chain = devnet_chain
    pool = _pool(chain)
    pool.add_transaction(_transfer(chain, protocol_accounts["alice"].address))
    full = pool.select_transactions(height=1)
    assert len(full) == 1
    assert pool.select_transactions(height=1, max_bytes=10) == []
    assert pool.select_transactions(height=1, limit=1) == full
    with pytest.raises(MempoolError, match="limit must be positive"):
        pool.select_transactions(height=1, limit=0)


def test_removal_and_expiry_rebuild_the_projection(
    devnet_chain: Any, protocol_accounts: Any
) -> None:
    chain = devnet_chain
    alice = protocol_accounts["alice"].address
    pool = _pool(chain)
    tx = _transfer(chain, alice)
    digest = pool.add_transaction(tx)
    pool.remove_transaction(digest)
    assert len(pool) == 0
    assert pool.projection.account(alice).balance == 0

    pool.add_transaction(tx)
    assert pool.prune(chain.params.mempool_ttl_blocks) == ()
    expired = pool.prune(chain.params.mempool_ttl_blocks + 2)
    assert expired == (digest,)
    assert len(pool) == 0
    assert pool.projection.account(alice).balance == 0


def test_reset_drops_what_the_new_tip_already_contains(
    devnet_chain: Any, protocol_accounts: Any
) -> None:
    chain = devnet_chain
    pool = _pool(chain)
    tx = _transfer(chain, protocol_accounts["alice"].address)
    pool.add_transaction(tx)
    tip = chain.state.copy()
    tip.ensure(tx.sender).nonce = 1
    pool.reset(tip)
    assert len(pool) == 0


def test_rebroadcast_reports_what_was_re_admitted(
    devnet_chain: Any, protocol_accounts: Any
) -> None:
    chain = devnet_chain
    pool = _pool(chain)
    tx = _transfer(chain, protocol_accounts["alice"].address)
    assert pool.rebroadcast((tx,), tip_state=chain.state) == (1, 0)
    assert pool.rebroadcast((tx,), tip_state=chain.state) == (0, 0), "already pending"
    stale_tip = chain.state.copy()
    stale_tip.ensure(tx.sender).nonce = 1
    assert pool.rebroadcast((tx,), tip_state=stale_tip) == (0, 0), "the nonce is spent"


def test_a_rematch_needs_a_cooldown_above_zero(live_chain: Any, chain_match: Any) -> None:
    """A zero cooldown disables the rematch rule; a positive one still rejects."""
    from sccp.chain.state import MinerStats

    chain = live_chain.chain
    assert chain.params.opponent_cooldown == 0
    proof = chain_match(chain, epoch=3).proof
    state = chain.state.copy()
    state.miner_stats[proof.winner] = MinerStats(
        wins=1, games=1, last_opponent_epoch={proof.loser: proof.epoch}
    )

    pool = Mempool(params=chain.params, tip_state=state, book=chain.book)
    assert pool.add_proof(proof) == proof.proof_id

    strict = chain.params.model_copy(update={"opponent_cooldown": 1})
    guarded = Mempool(params=strict, tip_state=state, book=chain.book)
    with pytest.raises(MempoolError, match="cooldown is 1 epochs"):
        guarded.add_proof(proof)


def test_proof_admission_reports_the_settlement_window(live_chain: Any, chain_match: Any) -> None:
    """A proof for a future epoch is refused with the height it becomes usable at."""
    chain = live_chain.chain
    match = chain_match(chain, epoch=3)
    pool = _pool(chain)
    early = match.proof.model_copy(update={"epoch": 99})
    with pytest.raises(MempoolError, match="settlement starts at height"):
        pool.add_proof(early)


def test_proof_admission_names_a_closed_claim_window(live_chain: Any, chain_match: Any) -> None:
    """A proof past its window is refused with the wording a miner drops it on."""
    chain = live_chain.chain
    proof = chain_match(chain, epoch=3).proof
    closed = chain.state.copy()
    closed.height = epoch_end_height(3, chain.params) + chain.params.claim_window + 1
    pool = Mempool(params=chain.params, tip_state=closed, book=chain.book)
    with pytest.raises(MempoolError, match="claim window, which closed at height"):
        pool.add_proof(proof)


def test_proof_admission_dedupes_ids_and_games(live_chain: Any, chain_match: Any) -> None:
    chain = live_chain.chain
    pool = _pool(chain)
    proof = chain_match(chain, epoch=3).proof
    assert pool.add_proof(proof) == proof.proof_id
    assert pool.has_proof(proof.proof_id) is True
    assert len(pool) == 1
    with pytest.raises(MempoolError, match="already in the mempool"):
        pool.add_proof(proof)
    same_game = proof.model_copy(update={"proof_id": "aa" * 32})
    with pytest.raises(MempoolError, match="already has a pending proof"):
        pool.add_proof(same_game)
    pool.remove_proof(proof.proof_id)
    assert len(pool) == 0


def test_proof_selection_filters_by_winner_and_count(live_chain: Any, chain_match: Any) -> None:
    chain = live_chain.chain
    match = chain_match(chain, epoch=3)
    pool = _pool(chain)
    pool.add_proof(match.proof)
    winner = match.proof.winner
    assert pool.select_proofs(winner, 1, height=4) == [match.proof]
    assert pool.select_proofs(winner, 0, height=4) == []
    assert pool.select_proofs("not-a-miner", 1, height=4) == []


def test_proof_selection_opens_after_the_epochs_last_block(
    live_chain: Any, chain_match: Any
) -> None:
    """A proof settles from the first block after its epoch, as consensus allows."""
    chain = live_chain.chain
    match = chain_match(chain, epoch=3)
    pool = _pool(chain)
    pool.add_proof(match.proof)
    last = epoch_end_height(3, chain.params)
    window = chain.params.claim_window
    assert pool.select_proofs(match.proof.winner, 1, height=last) == []
    assert pool.select_proofs(match.proof.winner, 1, height=last + 1) == [match.proof]
    assert pool.select_proofs(match.proof.winner, 1, height=last + window) == [match.proof]
    assert pool.select_proofs(match.proof.winner, 1, height=last + window + 1) == []


def test_reset_keeps_only_proofs_the_next_block_can_settle(
    live_chain: Any, chain_match: Any
) -> None:
    chain = live_chain.chain
    proof = chain_match(chain, epoch=3).proof
    last = epoch_end_height(3, chain.params)
    window = chain.params.claim_window

    def kept(height: int) -> bool:
        """Whether a pool re-based on a tip at ``height`` still offers the proof."""
        tip = chain.state.copy()
        tip.height = height
        pool = _pool(chain)
        pool.add_proof(proof)
        pool.reset(tip)
        return proof.proof_id in pool.proofs

    assert kept(last - 1) is False, "the next block is still epoch 3's last"
    assert kept(last) is True, "the next block opens epoch 4"
    assert kept(last + window - 1) is True, "the claim window is still open"
    assert kept(last + window) is False, "the claim window has closed"
