"""Fork choice, block storage in memory and reorg handling (plan section 5.11).

Every stored block keeps the state it produces. States are pure functions of the
ancestor chain, so a side branch is validated exactly like the main chain and a
reorg is just "adopt the other branch's tip". At prototype scale the memory cost
of that cache is irrelevant; :mod:`sccp.chain.storage` keeps the durable copy and
periodic snapshots for restarts.

Fork choice is ``block_work = (1 << seal_bits) + k * WIN_UNIT`` summed over the
branch, with ties broken by the lower block hash so two nodes always agree.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sccp.chain.block import ConnectedBlock, block_hash, connect_block
from sccp.chain.genesis import load_genesis
from sccp.chain.state import State
from sccp.chess.openings import OpeningBook
from sccp.config import NetworkParams, get_network
from sccp.errors import ChainError
from sccp.protocol.models import Block, BlockHeader, Transaction, WinProof


@dataclass(slots=True)
class StoredBlock:
    """A validated block plus the state it produces and its cumulative work."""

    block: Block
    block_hash: str
    parent_hash: str
    height: int
    chain_work: int
    state: State

    @property
    def header(self) -> BlockHeader:
        """The block header."""
        return self.block.header


@dataclass(frozen=True, slots=True)
class Reorg:
    """A fork-choice change: which blocks left the main chain and which joined."""

    old_tip: str
    new_tip: str
    orphaned: tuple[Block, ...]
    adopted: tuple[Block, ...]

    @property
    def orphaned_transactions(self) -> tuple[Transaction, ...]:
        """Transactions from the discarded branch, ready for rebroadcast."""
        return tuple(tx for block in self.orphaned for tx in block.transactions)

    @property
    def orphaned_proofs(self) -> tuple[WinProof, ...]:
        """Proofs from the discarded branch, ready for rebroadcast."""
        return tuple(proof for block in self.orphaned for proof in block.proofs)


@dataclass(slots=True)
class Chain:
    """An in-memory block tree rooted at a pinned genesis block."""

    params: NetworkParams
    genesis_block: Block
    genesis_state: State
    blocks: dict[str, StoredBlock] = field(default_factory=dict)
    orphans: dict[str, list[Block]] = field(default_factory=dict)
    book: OpeningBook | None = None
    _tip: str = ""

    def __post_init__(self) -> None:
        if self.genesis_state.height != 0:
            raise ChainError("the genesis state must be at height 0")
        root_hash = block_hash(self.genesis_block.header)
        if self.genesis_block.header.height != 0:
            raise ChainError("the genesis block must be at height 0")
        self.blocks[root_hash] = StoredBlock(
            block=self.genesis_block,
            block_hash=root_hash,
            parent_hash=self.genesis_block.header.prev_hash,
            height=0,
            chain_work=self.genesis_state.difficulty.work if self.genesis_state.difficulty else 0,
            state=self.genesis_state,
        )
        self._tip = root_hash

    @classmethod
    def from_genesis_file(
        cls,
        network: str | NetworkParams,
        path: str,
        *,
        params: NetworkParams | None = None,
    ) -> Chain:
        """Build a chain whose height-0 block comes from a genesis document."""
        resolved = params or get_network(network)
        block, state = load_genesis(path, resolved)
        return cls(params=resolved, genesis_block=block, genesis_state=state)

    # -- accessors ----------------------------------------------------------

    @property
    def genesis_hash(self) -> str:
        """The hash of the height-0 block."""
        return next(iter(self.blocks))

    @property
    def tip_hash(self) -> str:
        """The hash of the current best block."""
        return self._tip

    @property
    def tip(self) -> StoredBlock:
        """The current best block."""
        return self.blocks[self._tip]

    @property
    def height(self) -> int:
        """The height of the current best block."""
        return self.tip.height

    @property
    def state(self) -> State:
        """The state after applying the current best block."""
        return self.tip.state

    def get(self, block_hash_hex: str) -> StoredBlock | None:
        """The stored block with this hash, if any."""
        return self.blocks.get(block_hash_hex)

    def has(self, block_hash_hex: str) -> bool:
        """Whether this hash is stored."""
        return block_hash_hex in self.blocks

    def ancestors(self, block_hash_hex: str) -> list[StoredBlock]:
        """The block and all its ancestors, newest first."""
        path: list[StoredBlock] = []
        current = self.blocks.get(block_hash_hex)
        while current is not None:
            path.append(current)
            if current.height == 0:
                break
            current = self.blocks.get(current.parent_hash)
        return path

    def main_chain(self) -> list[StoredBlock]:
        """The adopted branch, genesis first."""
        return list(reversed(self.ancestors(self._tip)))

    def block_at_height(self, height: int) -> StoredBlock | None:
        """The adopted block at ``height``, if it exists."""
        if height < 0 or height > self.height:
            return None
        return self.main_chain()[height]

    def is_main_chain(self, block_hash_hex: str) -> bool:
        """Whether ``block_hash_hex`` is on the adopted branch."""
        if block_hash_hex not in self.blocks:
            return False
        return any(stored.block_hash == block_hash_hex for stored in self.ancestors(self._tip))

    # -- append and fork choice ---------------------------------------------

    def add_block(self, block: Block, *, now: int | None = None) -> Reorg | None:
        """Validate and store ``block``; return a :class:`Reorg` if the tip moves.

        A block whose parent is unknown is kept as an orphan and replayed when
        the parent arrives. Re-adding a known block is a no-op, which makes
        gossip and retries idempotent.
        """
        hash_hex = block_hash(block.header)
        if hash_hex in self.blocks:
            return None
        parent = self.blocks.get(block.header.prev_hash)
        if parent is None:
            self.orphans.setdefault(block.header.prev_hash, []).append(block)
            return None

        connected: ConnectedBlock = connect_block(
            block,
            params=self.params,
            parent_state=parent.state,
            parent_hash=parent.block_hash,
            now=now,
            book=self.book,
        )
        stored = StoredBlock(
            block=block,
            block_hash=hash_hex,
            parent_hash=parent.block_hash,
            height=block.header.height,
            chain_work=parent.chain_work + connected.work,
            state=connected.state,
        )
        self.blocks[hash_hex] = stored

        pending = self.orphans.pop(hash_hex, [])
        reorg = self._adopt_if_better(stored)
        for child in pending:
            child_reorg = self.add_block(child, now=now)
            if reorg is None and child_reorg is not None:
                reorg = child_reorg
        return reorg

    def add_blocks(
        self, blocks: list[Block] | tuple[Block, ...], *, now: int | None = None
    ) -> list[Reorg]:
        """Add several blocks in order and return the reorgs they caused."""
        reorgs: list[Reorg] = []
        for block in blocks:
            reorg = self.add_block(block, now=now)
            if reorg is not None:
                reorgs.append(reorg)
        return reorgs

    def pending_orphans(self) -> int:
        """How many blocks are waiting for an unknown parent."""
        return sum(len(items) for items in self.orphans.values())

    def _adopt_if_better(self, candidate: StoredBlock) -> Reorg | None:
        current = self.blocks[self._tip]
        if not self._better(candidate, current):
            return None
        old_tip = self._tip
        fork = self._fork_point(candidate, current)
        orphaned = tuple(
            stored.block
            for stored in reversed(self.ancestors(old_tip))
            if stored.height > fork.height
        )
        adopted = tuple(
            stored.block
            for stored in reversed(self.ancestors(candidate.block_hash))
            if stored.height > fork.height
        )
        self._tip = candidate.block_hash
        return Reorg(
            old_tip=old_tip,
            new_tip=self._tip,
            orphaned=orphaned,
            adopted=adopted,
        )

    def _fork_point(self, a: StoredBlock, b: StoredBlock) -> StoredBlock:
        """The highest block both branches have in common."""
        b_ancestors = {stored.block_hash for stored in self.ancestors(b.block_hash)}
        for stored in self.ancestors(a.block_hash):
            if stored.block_hash in b_ancestors:
                return stored
        raise ChainError("stored blocks do not share the genesis block")

    @staticmethod
    def _better(a: StoredBlock, b: StoredBlock) -> bool:
        """Heaviest cumulative work wins; ties break on the lower block hash."""
        return (a.chain_work, -int(a.block_hash, 16)) > (b.chain_work, -int(b.block_hash, 16))


def new_chain_from_document(
    network: str | NetworkParams,
    document: dict[str, object],
) -> Chain:
    """Build a chain directly from an in-memory genesis document."""
    from sccp.chain.genesis import parse_genesis_document

    params = get_network(network)
    block, state = parse_genesis_document(params, document)
    return Chain(params=params, genesis_block=block, genesis_state=state)


__all__ = [
    "Chain",
    "Reorg",
    "StoredBlock",
    "new_chain_from_document",
]
