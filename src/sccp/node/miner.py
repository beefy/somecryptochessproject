"""The node's optional settlement miner.

A full node does not need a key: it validates blocks and proofs for everyone.
For a small testnet it is still useful for the node to be able to assemble a
block around a proof a miner already announced, so a two-miner deployment can
keep making progress even when neither miner chooses to publish a block itself.

This miner deliberately does **not** search for games, own a bond, or invent
proofs. It only picks a proof already in the mempool, checks it is eligible at
the next height using the same :class:`~sccp.miner.block_builder.BlockBuilder`
the miner uses, and submits the resulting block to its own node.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass

from sccp.chain.block import bootstrap_end_height
from sccp.config import NetworkParams
from sccp.errors import ChainError, SccpError
from sccp.miner.block_builder import BlockBuilder
from sccp.node.service import NodeService, unix_now
from sccp.protocol.models import Block


@dataclass(slots=True)
class NodeMiner:
    """Mines one block at a time around proofs the node already holds."""

    service: NodeService
    address: str
    clock: Callable[[], int] = unix_now
    mine_bootstrap: bool = True
    """Whether to extend the empty bootstrap window on its own.

    A deployment that feeds a pre-built bootstrap chain to the node sets this
    to ``False`` so the background miner cannot race the bootstrap blocks.
    """
    min_interval_seconds: int = 0
    """Minimum wall-clock spacing between the blocks this miner builds.

    A block's timestamp is the wall clock and the difficulty retarget reads
    those timestamps: a miner that builds a block on every poll makes the chain
    look far faster than ``params.target_block_seconds``, and the schedule
    answers by raising ``wins_required``. On a small roster that is fatal -- one
    block then needs several wins by the same miner at the same height, more
    than an epoch can supply -- so a deployment paces its miner at the target
    spacing instead. ``0`` mines as fast as the pool allows (the default; the
    retarget stays neutral because blocks are then limited by the chess).
    """

    @property
    def params(self) -> NetworkParams:
        return self.service.params

    def _paced(self) -> bool:
        """Whether the tip is old enough for another block at this pace."""
        if self.min_interval_seconds <= 0:
            return True
        timestamps = self.service.chain.state.timestamps
        if not timestamps:
            return True
        return self.clock() - timestamps[-1] >= self.min_interval_seconds

    def _timestamp(self) -> int:
        tip_time = self.service.chain.state.timestamps[-1]
        return max(self.clock(), tip_time + 1)

    def _candidates(self, height: int) -> tuple[str, ...]:
        """The addresses to try as the block's miner, in order.

        Past the bootstrap window a block may only carry proofs won by its own
        miner (``check_proof``), so every announced winner is tried. The node's
        own address comes last: when no usable proof is pending the builder can
        still emit a heartbeat block, which carries transactions and mints
        nothing.
        """
        if height > bootstrap_end_height(self.params):
            winners = tuple(
                dict.fromkeys(
                    sorted(proof.winner for proof in self.service.mempool.proofs.values())
                )
            )
            return (*winners, self.address)
        if not self.mine_bootstrap:
            return ()
        return (self.address,)

    def mine_once(self) -> Block | None:
        """Build, ingest and return one block, or ``None`` if none is possible."""
        with self.service.write_lock:
            if not self._paced():
                return None
            height = self.service.height + 1
            for winner in self._candidates(height):
                try:
                    block = BlockBuilder(
                        params=self.params,
                        chain=self.service.chain,
                        mempool=self.service.mempool,
                        miner_address=winner,
                    ).build(timestamp=self._timestamp())
                except (ChainError, SccpError, ValueError):
                    continue
                if block is None:
                    continue
                ingested = self.service.ingest_block(block)
                if ingested.accepted:
                    return block
        return None

    def run(
        self,
        stop: threading.Event,
        *,
        poll_seconds: float = 1.0,
        on_block: Callable[[Block], None] | None = None,
    ) -> None:
        """Mine until ``stop`` is set, sleeping between attempts."""
        while not stop.wait(poll_seconds):
            try:
                block = self.mine_once()
                if block is not None and on_block is not None:
                    on_block(block)
            except Exception:  # pragma: no cover - a background thread must survive
                # A transient validation failure (for example a peer tip change)
                # must not kill the loop; the next poll starts from the new tip.
                continue


def proof_summary(block: Block) -> str:
    """A short log line for a settled block."""
    ids = ", ".join(proof.proof_id for proof in block.proofs)
    return f"height {block.header.height}: {len(block.proofs)} proof(s) {ids}"


__all__ = ["NodeMiner", "proof_summary"]
