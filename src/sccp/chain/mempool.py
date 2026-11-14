"""The mempool: transaction and proof pools (PROTOCOL.md sections 10 and 9).

Admission runs the full block checklist against a *projection* of the tip state,
so a transaction that depends on an earlier pending transaction from the same
sender is admitted and a transaction that double-spends the sender's projected
nonce is not. The pool keeps one entry per ``(sender, nonce)`` and one entry per
``game_hash``/``proof_id``, which is what makes "exactly one transaction per
``(from, nonce)``" true above the chain as well as on it.

Ordering is deterministic: senders take turns by the fee of their next
transaction (ties broken by address), and each sender's transactions stay in
nonce order. Two nodes with the same pool therefore build the same block body.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sccp.chain.block import check_proof
from sccp.chain.state import State, epoch_end_height
from sccp.chain.tx import apply_transaction, tx_size, txid, validate_transaction
from sccp.chess.openings import OpeningBook
from sccp.config import NetworkParams
from sccp.errors import LedgerError, MempoolError, TxValidationError
from sccp.protocol.models import BlockHeader, Transaction, WinProof


def _settleable(proof: WinProof, *, height: int, params: NetworkParams) -> bool:
    """Whether a block at ``height`` may settle ``proof`` (PROTOCOL.md 12.2).

    A proof is valid in blocks *after* its epoch: :func:`sccp.chain.block.check_proof`
    rejects ``proof.epoch >= header.epoch``, and the header at
    ``epoch_end_height(e)`` still belongs to epoch ``e``. Settlement therefore
    starts at the first block of epoch ``e + 1`` and closes
    ``claim_window`` blocks after the epoch's last one. Admission, selection and
    re-basing all use this one test, so the pool is never stricter than a block
    (which would strand a proof the next block would accept) and never looser
    (which would offer a builder a proof its block cannot carry).
    """
    ended = epoch_end_height(proof.epoch, params)
    return ended < height <= ended + params.claim_window


@dataclass(slots=True)
class MempoolEntry:
    """A pending transaction and the height it entered the pool at."""

    tx: Transaction
    txid: str
    added_height: int
    size: int


@dataclass(slots=True)
class Mempool:
    """Pending transactions and proofs for one chain tip."""

    params: NetworkParams
    tip_state: State
    book: OpeningBook | None = None
    transactions: dict[str, MempoolEntry] = field(default_factory=dict)
    proofs: dict[str, WinProof] = field(default_factory=dict)
    _game_hashes: set[str] = field(default_factory=set)
    _projection: State | None = None

    def __post_init__(self) -> None:
        self._projection = self.tip_state.copy()

    @property
    def projection(self) -> State:
        """The tip state with every pending transaction applied."""
        if self._projection is None:  # pragma: no cover - defensive
            self._projection = self.tip_state.copy()
        return self._projection

    def __len__(self) -> int:
        """Number of pending transactions plus pending proofs."""
        return len(self.transactions) + len(self.proofs)

    @property
    def next_height(self) -> int:
        """The height of the block these transactions would enter.

        Admission uses it rather than the tip height because epoch windows,
        maturity and median-time-past are judged against the block a transaction
        lands in; at an epoch boundary the two differ by one epoch.
        """
        return self.tip_state.height + 1

    @property
    def pending_transaction_count(self) -> int:
        """How many transactions wait in the pool (proofs are counted apart)."""
        return len(self.transactions)

    # -- transactions -------------------------------------------------------

    def add_transaction(self, tx: Transaction, *, height: int | None = None) -> str:
        """Admit a transaction and return its txid."""
        height = self.next_height if height is None else height
        digest = txid(tx)
        if digest in self.transactions:
            raise MempoolError(f"transaction {digest} is already in the mempool")
        for entry in self.transactions.values():
            if entry.tx.sender == tx.sender and entry.tx.nonce == tx.nonce:
                raise MempoolError(
                    f"{tx.sender} already has a pending transaction with nonce {tx.nonce}"
                )
        try:
            validate_transaction(tx, self.params, self.projection, height=height, for_mempool=True)
        except TxValidationError as exc:
            raise MempoolError(f"transaction rejected ({exc.reason}): {exc}") from exc
        size = tx_size(tx)
        self.transactions[digest] = MempoolEntry(tx=tx, txid=digest, added_height=height, size=size)
        apply_transaction(self.projection, tx, height=height)
        return digest

    def remove_transaction(self, digest: str) -> None:
        """Drop a transaction by txid; re-project the remaining pool."""
        if self.transactions.pop(digest, None) is not None:
            self._reproject()

    def has_transaction(self, digest: str) -> bool:
        """Whether this txid is pending."""
        return digest in self.transactions

    # -- proofs -------------------------------------------------------------

    def add_proof(self, proof: WinProof) -> str:
        """Admit a win proof after the same checks a block would apply.

        The proof is checked against a synthetic header for the next block won by
        ``proof.winner``, which is exactly the context a block builder would give
        it. Expensive chess replay therefore happens at admission, not at build
        time, and an invalid proof never reaches a block.
        """
        if proof.proof_id in self.proofs:
            raise MempoolError(f"proof {proof.proof_id} is already in the mempool")
        if proof.game_hash in self._game_hashes:
            raise MempoolError(f"game {proof.game_hash} already has a pending proof")
        state = self.tip_state
        if proof.network != self.params.network_id:
            raise MempoolError(f"proof is for {proof.network!r}")
        next_height = state.height + 1
        if not _settleable(proof, height=next_height, params=self.params):
            ended = epoch_end_height(proof.epoch, self.params)
            if next_height <= ended:
                # Too early: the proof only settles once its epoch has ended, and
                # the node answers with the height it becomes usable at so a miner
                # can hold on to it instead of dropping it (PROTOCOL.md 12.2).
                raise MempoolError(
                    f"proof references epoch {proof.epoch}, which ends at height {ended}; "
                    f"settlement starts at height {ended + 1} and the next block is "
                    f"{next_height}"
                )
            raise MempoolError(
                f"win from height {ended} is older than the {self.params.claim_window}-block "
                f"claim window, which closed at height {ended + self.params.claim_window}; "
                f"the next block is {next_height}"
            )
        synthetic = BlockHeader(
            version=1,
            network=self.params.network_id,
            height=next_height,
            prev_hash=state.tip_hash,
            merkle_root="00" * 32,
            state_root="00" * 32,
            roster_root="00" * 32,
            timestamp=state.timestamps[-1] if state.timestamps else 0,
            epoch=next_height // self.params.epoch_length,
            beacon_hash="00" * 32,
            wins_required=self.params.wins_required,
            seal_bits=self.params.seal_bits,
            seal_nonce=0,
            miner=proof.winner,
        )
        try:
            check_proof(
                state,
                self.params,
                synthetic,
                proof,
                opponents=frozenset(item.loser for item in self.proofs.values()),
                game_hashes=state.known_game_hashes() | frozenset(self._game_hashes),
                proof_ids=state.known_proof_ids() | frozenset(self.proofs),
                book=self.book,
            )
        except LedgerError as exc:
            raise MempoolError(f"proof rejected: {exc}") from exc
        self.proofs[proof.proof_id] = proof
        self._game_hashes.add(proof.game_hash)
        return proof.proof_id

    def remove_proof(self, proof_id: str) -> None:
        """Drop a proof by id."""
        proof = self.proofs.pop(proof_id, None)
        if proof is not None:
            self._game_hashes.discard(proof.game_hash)

    def has_proof(self, proof_id: str) -> bool:
        """Whether this proof id is pending."""
        return proof_id in self.proofs

    # -- selection ----------------------------------------------------------

    def select_transactions(
        self, *, height: int, max_bytes: int | None = None, limit: int | None = None
    ) -> list[Transaction]:
        """A deterministic block body of transactions that fit the size cap.

        ``limit`` caps how many transactions the body may carry; a proofless
        heartbeat block asks for one at a time.
        """
        if limit is not None and limit < 1:
            raise MempoolError(f"limit must be positive, not {limit}")
        cap = self.params.max_block_bytes if max_bytes is None else max_bytes
        queues: dict[str, list[MempoolEntry]] = {}
        for entry in self.transactions.values():
            queues.setdefault(entry.tx.sender, []).append(entry)
        for entries in queues.values():
            entries.sort(key=lambda entry: entry.tx.nonce)
        work = self.tip_state.copy()
        chosen: list[Transaction] = []
        used = 0
        deferred: set[str] = set()
        progressed = True
        while True:
            if limit is not None and len(chosen) >= limit:
                break
            ready = [
                (entries[0].tx.fee, entries[0].tx.sender, entries)
                for entries in queues.values()
                if entries and entries[0].tx.sender not in deferred
            ]
            if not ready:
                if deferred and progressed:
                    # A sender we deferred may be payable now that somebody else
                    # applied. Retry once per round of progress, then stop: the
                    # pool never drops a still-valid transaction just because it
                    # was ready out of order.
                    deferred.clear()
                    progressed = False
                    continue
                break
            ready.sort(key=lambda item: (-item[0], item[1]))
            entries = ready[0][2]
            entry = entries[0]
            if used + entry.size > cap:
                queues.pop(entry.tx.sender, None)
                continue
            try:
                validate_transaction(entry.tx, self.params, work, height=height)
            except TxValidationError:
                deferred.add(entry.tx.sender)
                continue
            apply_transaction(work, entry.tx, height=height)
            chosen.append(entry.tx)
            used += entry.size
            entries.pop(0)
            progressed = True
        return chosen

    def select_proofs(self, miner: str, count: int, *, height: int) -> list[WinProof]:
        """Up to ``count`` pending proofs ``miner`` could settle at ``height``.

        Eligibility is the block checklist's, not a stricter rule of the pool's
        own: a game from the epoch that ``height`` belongs to is settleable once
        ``height`` is that epoch's last block (:func:`_settleable`).
        """
        live = [
            proof
            for proof in self.proofs.values()
            if proof.winner == miner and _settleable(proof, height=height, params=self.params)
        ]
        live.sort(key=lambda proof: proof.proof_id)
        return live[:count]

    # -- maintenance --------------------------------------------------------

    def prune(self, height: int) -> tuple[str, ...]:
        """Drop expired transactions and return the txids that were removed."""
        ttl = self.params.mempool_ttl_blocks
        expired = [
            digest
            for digest, entry in self.transactions.items()
            if height - entry.added_height > ttl
        ]
        for digest in expired:
            self.transactions.pop(digest, None)
        if expired:
            self._reproject()
        return tuple(expired)

    def reset(self, tip_state: State) -> None:
        """Re-base the pool on a new tip, dropping whatever no longer applies."""
        survivors = sorted(
            (entry.tx for entry in self.transactions.values()),
            key=lambda tx: (tx.sender, tx.nonce),
        )
        self.tip_state = tip_state
        self.transactions.clear()
        next_height = tip_state.height + 1
        stale = [
            proof_id
            for proof_id, proof in self.proofs.items()
            if not _settleable(proof, height=next_height, params=self.params)
        ]
        for proof_id in stale:
            self.proofs.pop(proof_id, None)
        self._game_hashes = {proof.game_hash for proof in self.proofs.values()}
        self._projection = tip_state.copy()
        for tx in survivors:
            try:
                self.add_transaction(tx, height=self.next_height)
            except MempoolError:
                continue

    def rebroadcast(
        self,
        transactions: tuple[Transaction, ...] = (),
        proofs: tuple[WinProof, ...] = (),
        *,
        tip_state: State,
    ) -> tuple[int, int]:
        """Re-base on a new tip and re-admit orphaned items; return (txs, proofs)."""
        self.reset(tip_state)
        admitted_tx = 0
        admitted_proof = 0
        for tx in transactions:
            try:
                self.add_transaction(tx)
            except MempoolError:
                continue
            admitted_tx += 1
        for proof in proofs:
            try:
                self.add_proof(proof)
            except MempoolError:
                continue
            admitted_proof += 1
        return admitted_tx, admitted_proof

    def _reproject(self) -> None:
        projection = self.tip_state.copy()
        height = self.next_height
        for entry in sorted(
            self.transactions.values(), key=lambda item: (item.tx.sender, item.tx.nonce)
        ):
            try:
                validate_transaction(entry.tx, self.params, projection, height=height)
                apply_transaction(projection, entry.tx, height=height)
            except TxValidationError:
                self.transactions.pop(entry.txid, None)
        self._projection = projection


__all__ = ["Mempool", "MempoolEntry"]
