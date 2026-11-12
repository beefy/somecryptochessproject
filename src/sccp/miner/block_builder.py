"""Assemble a claim into a block (plan sections 5.10 and 5.11).

The builder never invents consensus: it computes the scheduled difficulty, asks
the mempool for ``k`` proofs won by this miner, applies the body to a copy of the
tip state to learn the resulting ``state_root``, and only then writes the header
and seals it.

A miner with fewer than ``k`` eligible proofs gets ``None`` rather than an invalid
block: the protocol stalls instead of lowering ``k``.
"""

from __future__ import annotations

from dataclasses import dataclass

from sccp.chain.block import (
    HEADER_VERSION,
    apply_block_body,
    block_hash,
    block_subsidy,
    epoch_beacon,
    merkle_root_of,
    required_proofs,
)
from sccp.chain.chain import Chain, Reorg
from sccp.chain.difficulty import Difficulty, next_difficulty, seal_valid
from sccp.chain.mempool import Mempool
from sccp.chain.state import MAX_RECENT_TIMESTAMPS, ZERO_HASH, EpochRecord
from sccp.chain.tx import make_coinbase
from sccp.config import NetworkParams
from sccp.crypto.address import encode_address
from sccp.crypto.keys import Keypair
from sccp.errors import ChainError
from sccp.protocol.models import Block, BlockHeader

MAX_SEAL_ATTEMPTS: int = 1 << 24
"""Upper bound on seal nonces before giving up; ``seal_bits = 0`` needs one."""


@dataclass(slots=True)
class BlockBuilder:
    """Builds the next block for one miner from a chain and a mempool."""

    params: NetworkParams
    chain: Chain
    mempool: Mempool
    keypair: Keypair

    @property
    def miner(self) -> str:
        """The address the coinbase pays and every proof must belong to."""
        return encode_address(self.keypair.public_key_bytes, self.params)

    def next_difficulty(self, height: int) -> Difficulty:
        """The difficulty the block at ``height`` must declare."""
        state = self.chain.state
        parent = state.difficulty
        if parent is None:  # pragma: no cover - state always sets it
            parent = Difficulty(
                wins_required=self.params.wins_required, seal_bits=self.params.seal_bits
            )
        return next_difficulty(
            self.params,
            parent,
            height=height,
            timestamps=state.timestamps,
            last_k_change_height=state.last_k_change_height,
        )

    def _epoch_view(self, epoch: int, height: int, prev_hash: str) -> tuple[str, str]:
        """The beacon hash and roster root a block at ``height`` must declare."""
        params = self.params
        probe = self.chain.state.copy()
        record: EpochRecord | None
        if height % params.epoch_length == 0:
            beacon = epoch_beacon(params, probe, epoch, prev_hash)
            record = probe.open_epoch(epoch, beacon=beacon, height=height)
        else:
            record = probe.epochs.get(epoch)
            if record is None:
                raise ChainError(f"epoch {epoch} was never opened")
        beacon_hash = record.beacon.hash if record.beacon is not None else ZERO_HASH
        return beacon_hash, record.roster_root

    def _seal(self, header: BlockHeader) -> BlockHeader:
        """Increment ``seal_nonce`` until the header meets its seal target."""
        for nonce in range(MAX_SEAL_ATTEMPTS):
            candidate = header.model_copy(update={"seal_nonce": nonce})
            if seal_valid(block_hash(candidate), header.seal_bits):
                return candidate
        raise ChainError(f"no seal nonce under {MAX_SEAL_ATTEMPTS} meets {header.seal_bits} bits")

    def build(self, *, timestamp: int, now: int | None = None) -> Block | None:
        """Build the next block, or ``None`` when this miner cannot claim one.

        ``now`` is accepted for symmetry with
        :meth:`sccp.chain.chain.Chain.add_block` and is not used to choose the
        timestamp; the caller owns that.
        """
        del now
        params = self.params
        height = self.chain.height + 1
        miner = self.miner
        difficulty = self.next_difficulty(height)

        needed = required_proofs(params, height, difficulty.wins_required)
        proofs = tuple(self.mempool.select_proofs(miner, needed, height=height))
        if len(proofs) < needed:
            return None
        transactions = tuple(self.mempool.select_transactions(height=height))

        epoch = height // params.epoch_length
        prev_hash = self.chain.tip_hash
        beacon_hash, roster_root = self._epoch_view(epoch, height, prev_hash)
        provisional = BlockHeader(
            version=HEADER_VERSION,
            network=params.network_id,
            height=height,
            prev_hash=prev_hash,
            merkle_root=ZERO_HASH,
            state_root=ZERO_HASH,
            roster_root=roster_root,
            timestamp=timestamp,
            epoch=epoch,
            beacon_hash=beacon_hash,
            wins_required=difficulty.wins_required,
            seal_bits=difficulty.seal_bits,
            seal_nonce=0,
            miner=miner,
        )

        work = self.chain.state.copy()
        work.height = height
        work.timestamps = [*work.timestamps, timestamp][-MAX_RECENT_TIMESTAMPS:]
        fees = apply_block_body(
            work, params, provisional, transactions, proofs, book=self.chain.book
        )
        subsidy = block_subsidy(params, height, self.chain.state.minted)
        value = subsidy + fees
        coinbase = make_coinbase(miner, value, params.network_id)
        work.credit_coinbase(miner, value, height)

        draft = Block(
            header=provisional,
            coinbase=coinbase,
            transactions=transactions,
            proofs=proofs,
        )
        header = provisional.model_copy(
            update={"merkle_root": merkle_root_of(draft), "state_root": work.state_root()}
        )
        return Block(
            header=self._seal(header),
            coinbase=coinbase,
            transactions=transactions,
            proofs=proofs,
        )

    def build_and_append(self, *, timestamp: int) -> tuple[Block, Reorg | None] | None:
        """Build the next block and append it to the chain."""
        block = self.build(timestamp=timestamp)
        if block is None:
            return None
        return block, self.chain.add_block(block)


__all__ = ["MAX_SEAL_ATTEMPTS", "BlockBuilder"]
