"""Header and block validation (PROTOCOL.md sections 9 and 12).

``connect_block`` is the single entry point that turns a parent state plus a
block into a child state. It runs cheap checks first and expensive chess replay
last, in this order:

1. header shape, network, height, parent hash;
2. timestamp against median time past and the two-hour future bound;
3. epoch arithmetic, the epoch beacon and the roster root;
4. declared ``wins_required``/``seal_bits`` against the retarget schedule;
5. the seal;
6. body size, tx ids, merkle root;
7. coinbase maturity, transactions, proofs (the expensive part), coinbase value;
8. ``state_root`` against the state the block actually produces.

A block that reaches step 8 but disagrees on ``state_root`` is invalid, not
"almost valid": the root is what a light client would trust later.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final, NoReturn

from sccp.chain.difficulty import Difficulty, next_difficulty, seal_valid
from sccp.chain.state import (
    MAX_RECENT_TIMESTAMPS,
    ZERO_HASH,
    EpochRecord,
    MatchRecord,
    ProofRecord,
    State,
    epoch_end_height,
    epoch_of,
    first_matchable_epoch,
)
from sccp.chain.tx import (
    apply_transaction,
    tx_size,
    txid,
    validate_transaction,
)
from sccp.chess.openings import OpeningBook, book_for
from sccp.config import NetworkParams
from sccp.errors import (
    BlockValidationError,
    OpeningBookError,
    ProofVerificationError,
    TimeoutClaimError,
    TxValidationError,
)
from sccp.protocol.beacon import MTP_WINDOW, compute_beacon, median_time_past
from sccp.protocol.models import (
    Beacon,
    BeaconReveal,
    Block,
    BlockHeader,
    Transaction,
    WinProof,
)
from sccp.protocol.pairing import RosterEntry
from sccp.protocol.verification import ProofContext, verify_win_proof
from sccp.serialization import Domain, canonical_json, domain_hex, merkle_leaf, merkle_node

HEADER_VERSION: Final = 1
"""The only block header version this implementation accepts."""

MAX_FUTURE_SECONDS: Final = 2 * 60 * 60
"""A timestamp may be at most two hours ahead of the verifier's clock."""


class BlockFailure(StrEnum):
    """Stable machine-readable reasons a block can fail."""

    SCHEMA = "schema"
    NETWORK = "network"
    HEIGHT = "height"
    PREV_HASH = "prev-hash"
    MERKLE = "merkle"
    STATE_ROOT = "state-root"
    ROSTER_ROOT = "roster-root"
    TIMESTAMP = "timestamp"
    EPOCH = "epoch"
    BEACON = "beacon"
    DIFFICULTY = "difficulty"
    SEAL = "seal"
    SIZE = "size"
    COINBASE = "coinbase"
    PROOFS = "proofs"
    TRANSACTIONS = "transactions"
    DUPLICATE = "duplicate"
    GENESIS = "genesis"


def _fail(reason: BlockFailure, message: str) -> NoReturn:
    raise BlockValidationError(reason, message)


def header_payload(header: BlockHeader) -> dict[str, object]:
    """The exact mapping a header hash commits to."""
    return {
        "version": header.version,
        "network": header.network,
        "height": header.height,
        "prev_hash": header.prev_hash,
        "merkle_root": header.merkle_root,
        "state_root": header.state_root,
        "roster_root": header.roster_root,
        "timestamp": header.timestamp,
        "epoch": header.epoch,
        "beacon_hash": header.beacon_hash,
        "wins_required": header.wins_required,
        "seal_bits": header.seal_bits,
        "seal_nonce": header.seal_nonce,
        "miner": header.miner,
    }


def block_hash(header: BlockHeader) -> str:
    """``H("sccp/v1/header", header)`` as lowercase hex."""
    return domain_hex(Domain.HEADER, header_payload(header))


def body_root(items: list[str] | tuple[str, ...]) -> str:
    """Merkle root of an ordered list of txids/proof ids.

    The shared construction is keyed by a string, so the key is the
    item's position: leaves are ``sha256(0x00 || str(i) || 0x00 || json(item))``
    in *given* order (no sorting), an odd node is promoted unchanged.
    """
    if not items:
        return ZERO_HASH
    level = [merkle_leaf(str(index), item) for index, item in enumerate(items)]
    while len(level) > 1:
        level = [
            merkle_node(level[i], level[i + 1]) if i + 1 < len(level) else level[i]
            for i in range(0, len(level), 2)
        ]
    return level[0].hex()


def merkle_root_of(block: Block) -> str:
    """``[coinbase_txid, txids..., proof_ids...]`` in that order (plan 5.11)."""
    items: list[str] = []
    if block.coinbase is not None:
        items.append(txid(block.coinbase))
    items.extend(txid(tx) for tx in block.transactions)
    items.extend(proof.proof_id for proof in block.proofs)
    return body_root(items)


def block_size(block: Block) -> int:
    """Canonical byte size of the whole block."""
    return len(canonical_json(block.model_dump(mode="json", by_alias=True)))


def block_subsidy(params: NetworkParams, height: int, minted: int) -> int:
    """``subsidy >> (height // halving_interval)``, clamped to the supply cap."""
    if params.halving_interval is None:
        amount = params.subsidy
    else:
        amount = params.subsidy >> (height // params.halving_interval)
    cap = params.max_money
    if cap is not None:
        amount = min(amount, max(0, cap - minted))
    return amount


def bootstrap_end_height(params: NetworkParams) -> int:
    """Last height at which a block may carry no proofs at all.

    Availability is committed two epochs ahead (plan 5.5.8), so epoch 2 is the
    first nominal matchable epoch but no availability transaction for it can
    exist: epoch 0 has no antecedent. Nothing is lost -- the reveal for epoch 2
    would have to be committed in epoch 0 as well -- so the first epoch that can
    actually hold a roster is ``first_matchable_epoch + 1``.

    Proofs additionally may only reference an epoch that has *ended*
    (plan 5.10), so the first block that can carry a proof is the first block of
    the epoch after that. Both facts collapse into this one bound: up to and
    including the last block of the first realizable matchable epoch ``k`` cannot
    be met by anybody, and demanding it would deadlock a fresh chain forever.
    "The protocol stalls rather than lowering ``k``" (plan 5.10) is about roster
    depth; it cannot mean a chain that can never leave genesis.

    The exemption is a pure function of the parameters and height, so every node
    derives the same answer and no miner can extend it.
    """
    return epoch_end_height(first_matchable_epoch(params) + 1, params)


def required_proofs(params: NetworkParams, height: int, wins_required: int) -> int:
    """How many proofs a block at ``height`` must carry (zero during bootstrap)."""
    if height <= bootstrap_end_height(params):
        return 0
    return wins_required


@dataclass(frozen=True, slots=True)
class ConnectedBlock:
    """A block that passed every check, with the state it produces."""

    block: Block
    block_hash: str
    work: int
    state: State


def epoch_beacon(
    params: NetworkParams,
    state: State,
    epoch: int,
    parent_hash: str,
) -> Beacon | None:
    """``B_e`` from the reveals recorded for ``epoch`` and the hash at ``H_e``.

    ``state`` must be the state at ``H_e`` (the parent of the epoch's first
    block). Epochs before :func:`first_matchable_epoch` have no beacon at all.
    """
    if epoch < first_matchable_epoch(params):
        return None
    availability = state.availability.get(epoch, {})
    entries: list[BeaconReveal] = []
    for miner, nonce in sorted(state.reveals.get(epoch, {}).items()):
        committed = availability.get(miner)
        if committed is None:
            continue
        entries.append(BeaconReveal(miner=miner, commitment=committed.commitment, nonce=nonce))
    return compute_beacon(
        network=params.network_id,
        epoch=epoch,
        block_hash=parent_hash,
        reveals=entries,
    )


def _check_header_shape(
    block: Block,
    params: NetworkParams,
    parent_state: State,
    parent_hash: str,
) -> None:
    header = block.header
    if header.version != HEADER_VERSION:
        _fail(BlockFailure.SCHEMA, f"unsupported header version {header.version}")
    if header.network != params.network_id:
        _fail(
            BlockFailure.NETWORK,
            f"block is for {header.network!r}, this verifier is on {params.network_id!r}",
        )
    if header.height != parent_state.height + 1:
        _fail(
            BlockFailure.HEIGHT,
            f"block height {header.height} does not follow parent height {parent_state.height}",
        )
    if header.prev_hash != parent_hash:
        _fail(
            BlockFailure.PREV_HASH,
            f"block names parent {header.prev_hash}, expected {parent_hash}",
        )
    if header.epoch != epoch_of(header.height, params):
        _fail(
            BlockFailure.EPOCH,
            f"height {header.height} is in epoch {epoch_of(header.height, params)}, "
            f"header declares {header.epoch}",
        )


def _check_timestamp(block: Block, parent_state: State, now: int | None) -> None:
    header = block.header
    if parent_state.timestamps:
        median = median_time_past(parent_state.timestamps[-MTP_WINDOW:])
        if header.timestamp <= median:
            _fail(
                BlockFailure.TIMESTAMP,
                f"timestamp {header.timestamp} is not after the median time past {median}",
            )
    if now is not None and header.timestamp > now + MAX_FUTURE_SECONDS:
        _fail(
            BlockFailure.TIMESTAMP,
            f"timestamp {header.timestamp} is more than {MAX_FUTURE_SECONDS} s in the future",
        )


def _check_difficulty(block: Block, params: NetworkParams, parent_state: State) -> Difficulty:
    parent = parent_state.difficulty or Difficulty(
        wins_required=params.wins_required, seal_bits=params.seal_bits
    )
    expected = next_difficulty(
        params,
        parent,
        height=block.header.height,
        timestamps=parent_state.timestamps,
        last_k_change_height=parent_state.last_k_change_height,
    )
    header = block.header
    if (header.wins_required, header.seal_bits) != (
        expected.wins_required,
        expected.seal_bits,
    ):
        _fail(
            BlockFailure.DIFFICULTY,
            f"header declares k={header.wins_required} seal_bits={header.seal_bits}, "
            f"the schedule says k={expected.wins_required} seal_bits={expected.seal_bits}",
        )
    return expected


def _proof_context(
    work: State,
    params: NetworkParams,
    record_epoch: int,
    book: OpeningBook | None = None,
) -> ProofContext:
    record = work.epochs.get(record_epoch)
    if record is None or record.beacon is None:
        _fail(BlockFailure.PROOFS, f"no beacon recorded for epoch {record_epoch}")
    if book is None:
        try:
            book = book_for(params)
        except OpeningBookError:
            book = None
    return ProofContext(
        params=params,
        beacon=record.beacon,
        roster={
            address: RosterEntry(address=address, max_games=member.max_games)
            for address, member in record.roster.items()
        },
        public_keys=work.public_keys(),
        book=book,
        quotas=record.quotas,
        availability=record.availability,
        seen_game_hashes=work.known_game_hashes(),
        seen_proof_ids=work.known_proof_ids(),
    )


def check_proof(
    work: State,
    params: NetworkParams,
    header: BlockHeader,
    proof: WinProof,
    *,
    opponents: frozenset[str],
    game_hashes: frozenset[str],
    proof_ids: frozenset[str],
    book: OpeningBook | None = None,
) -> None:
    """Run every proof rule that needs the chain, not just the proof."""
    if proof.network != params.network_id:
        _fail(BlockFailure.PROOFS, f"proof is for {proof.network!r}")
    if proof.winner != header.miner:
        _fail(
            BlockFailure.PROOFS,
            f"proof winner {proof.winner} is not the block miner {header.miner}",
        )
    if proof.epoch >= header.epoch:
        _fail(
            BlockFailure.PROOFS,
            f"proof references epoch {proof.epoch}, which has not ended before epoch "
            f"{header.epoch}",
        )
    record = work.epochs.get(proof.epoch)
    if record is None or not record.matchable:
        _fail(BlockFailure.PROOFS, f"epoch {proof.epoch} has no matchable roster")
    if proof.winner not in record.roster:
        _fail(
            BlockFailure.PROOFS,
            f"winner {proof.winner} was not on the epoch {proof.epoch} roster",
        )
    opponent = proof.loser
    if opponent in opponents:
        _fail(
            BlockFailure.PROOFS,
            f"opponent {opponent} appears twice in one block; proofs need distinct opponents",
        )
    if proof.game_hash in game_hashes:
        _fail(BlockFailure.DUPLICATE, f"game {proof.game_hash} is already on this chain")
    if proof.proof_id in proof_ids:
        _fail(BlockFailure.DUPLICATE, f"proof {proof.proof_id} is already on this chain")

    ended = epoch_end_height(proof.epoch, params)
    if header.height < ended:
        _fail(BlockFailure.PROOFS, f"epoch {proof.epoch} ends at height {ended}")
    if header.height > ended + params.claim_window:
        _fail(
            BlockFailure.PROOFS,
            f"win from height {ended} is older than the {params.claim_window}-block claim window",
        )
    stats = work.miner_stats.get(proof.winner)
    # A zero cooldown disables the rematch rule (test networks run a tiny roster
    # that must be able to settle every game it plays; mainnet sets a cooldown).
    if stats is not None and params.opponent_cooldown > 0:
        last = stats.last_opponent_epoch.get(opponent)
        if last is not None and proof.epoch - last <= params.opponent_cooldown:
            _fail(
                BlockFailure.PROOFS,
                f"{proof.winner} played {opponent} in epoch {last}; the cooldown is "
                f"{params.opponent_cooldown} epochs",
            )
    if proof.low_ply != (proof.plies < params.min_plies):
        _fail(
            BlockFailure.PROOFS,
            f"low_ply is {proof.low_ply} for a {proof.plies}-ply game "
            f"(min_plies {params.min_plies})",
        )
    if proof.termination != "checkmate" and proof.move_evidence:
        _fail(BlockFailure.PROOFS, "move_evidence is only valid for a checkmate finish")
    if proof.termination == "time_forfeit" and proof.clock_evidence is None:
        _fail(BlockFailure.PROOFS, "a time forfeit needs clock_evidence")
    if proof.termination == "time_forfeit" and proof.timeout_claim_id is not None:
        claim = work.timeout_claims.get(proof.timeout_claim_id)
        if claim is None:
            _fail(BlockFailure.PROOFS, f"unknown timeout claim {proof.timeout_claim_id}")
        if claim.certificate != proof.certificate:
            _fail(BlockFailure.PROOFS, "the timeout claim carries a different certificate")
        if claim.claimant != proof.winner:
            _fail(BlockFailure.PROOFS, "the timeout claim was opened by the other seat")
        if claim.disputed_height is not None:
            _fail(BlockFailure.PROOFS, "the timeout claim was disputed")

    try:
        verify_win_proof(proof, _proof_context(work, params, proof.epoch, book))
    except ProofVerificationError as exc:
        _fail(BlockFailure.PROOFS, f"proof {proof.proof_id} failed verification: {exc}")


def _apply_proof(
    work: State,
    params: NetworkParams,
    header: BlockHeader,
    proof: WinProof,
) -> None:
    """Register an already-checked proof and update the miner statistics."""
    from sccp.chain import timeout

    seats = tuple(sorted((proof.certificate.white, proof.certificate.black)))
    work.proofs[proof.game_hash] = ProofRecord(
        proof_id=proof.proof_id,
        game_hash=proof.game_hash,
        height=header.height,
        winner=proof.winner,
        miner=header.miner,
    )
    work.proof_ids[proof.proof_id] = proof.game_hash
    work.matches[proof.match_id] = MatchRecord(
        epoch=proof.epoch,
        round=proof.round,
        seats=(seats[0], seats[1]),
    )
    for address, delta in ((proof.winner, 1), (proof.loser, 0)):
        stats = work.stats(address)
        stats.games += 1
        stats.wins += delta
        stats.last_opponent_epoch[proof.loser if delta else proof.winner] = proof.epoch
    if proof.termination == "time_forfeit" and proof.timeout_claim_id is not None:
        try:
            timeout.finalise_claim(work, proof.timeout_claim_id, header.height)
        except TimeoutClaimError as exc:
            _fail(BlockFailure.PROOFS, f"timeout claim cannot finalise: {exc}")


def _check_body(work: State, params: NetworkParams, block: Block) -> None:
    header = block.header
    if block.coinbase is None:
        _fail(BlockFailure.COINBASE, "every non-genesis block carries exactly one coinbase")
    if block.coinbase.signature is not None:
        _fail(BlockFailure.COINBASE, "a coinbase is authorised by the seal, not a signature")
    if block.coinbase.type.value != "transfer":
        _fail(BlockFailure.COINBASE, "the coinbase must be a transfer to the miner")
    if block.coinbase.sender != header.miner:
        _fail(BlockFailure.COINBASE, "the coinbase must pay the block miner")
    if block.coinbase.nonce != 0 or block.coinbase.fee != 0:
        _fail(BlockFailure.COINBASE, "the coinbase has no nonce and no fee")
    if block.coinbase.network != params.network_id:
        _fail(BlockFailure.COINBASE, "the coinbase must name the chain's network")
    if block.coinbase.payload.get("to") != header.miner:
        _fail(BlockFailure.COINBASE, "the coinbase must pay the block miner")
    if len(block.proofs) != required_proofs(params, header.height, header.wins_required):
        _fail(
            BlockFailure.PROOFS,
            f"block carries {len(block.proofs)} proofs, height {header.height} requires "
            f"{required_proofs(params, header.height, header.wins_required)}",
        )
    size = block_size(block)
    if size > params.max_block_bytes:
        _fail(BlockFailure.SIZE, f"block is {size} bytes, cap is {params.max_block_bytes}")
    if header.merkle_root != merkle_root_of(block):
        _fail(BlockFailure.MERKLE, "merkle_root does not match the block body")

    ids = [txid(block.coinbase), *(txid(tx) for tx in block.transactions)]
    if len(set(ids)) != len(ids):
        _fail(BlockFailure.DUPLICATE, "the block repeats a transaction id")
    for tx in block.transactions:
        if tx.nonce == 0 and tx.sender == header.miner and tx.type.value == "transfer":
            _fail(BlockFailure.DUPLICATE, "a body transaction imitates the coinbase")
        if tx_size(tx) > params.max_tx_bytes:
            _fail(BlockFailure.TRANSACTIONS, "a body transaction exceeds max_tx_bytes")
        if tx.sender == "" or not tx.signature:
            _fail(BlockFailure.TRANSACTIONS, "every body transaction must be signed")
    if len(block.proofs) != len({proof.proof_id for proof in block.proofs}):
        _fail(BlockFailure.DUPLICATE, "the block repeats a proof id")
    if len(block.proofs) != len({proof.game_hash for proof in block.proofs}):
        _fail(BlockFailure.DUPLICATE, "the block repeats a game hash")

    ordered = [proof.proof_id for proof in block.proofs]
    if ordered != sorted(ordered):
        _fail(BlockFailure.PROOFS, "proofs must be ordered by proof_id")


def apply_block_body(
    work: State,
    params: NetworkParams,
    header: BlockHeader,
    transactions: tuple[Transaction, ...] = (),
    proofs: tuple[WinProof, ...] = (),
    *,
    book: OpeningBook | None = None,
) -> int:
    """Apply maturity, transactions and proofs to ``work``; return total fees.

    ``work`` must already carry the block's height, timestamp and tip so that
    window rules, maturity and median-time-past behave as they will in
    :func:`connect_block`. This is the shared path for validation and for the
    block builder, so a builder can compute ``state_root`` before it has a header
    to put it in.
    """
    work.release_matured(header.height)
    fees = 0
    for tx in transactions:
        try:
            validate_transaction(tx, params, work, height=header.height)
        except TxValidationError as exc:
            _fail(
                BlockFailure.TRANSACTIONS,
                f"transaction {txid(tx)} is invalid at height {header.height}: {exc}",
            )
        apply_transaction(work, tx, height=header.height)
        fees += tx.fee

    opponents: set[str] = set()
    game_hashes = set(work.proofs)
    proof_ids = set(work.proof_ids)
    for proof in proofs:
        check_proof(
            work,
            params,
            header,
            proof,
            opponents=frozenset(opponents),
            game_hashes=frozenset(game_hashes),
            proof_ids=frozenset(proof_ids),
            book=book,
        )
        _apply_proof(work, params, header, proof)
        opponents.add(proof.loser)
        game_hashes.add(proof.game_hash)
        proof_ids.add(proof.proof_id)
    return fees


def connect_block(
    block: Block,
    *,
    params: NetworkParams,
    parent_state: State,
    parent_hash: str,
    now: int | None = None,
    book: OpeningBook | None = None,
) -> ConnectedBlock:
    """Validate ``block`` against its parent and return the child state.

    ``now`` is the verifier's Unix time; it is only used for the two-hour future
    bound and may be ``None`` in deterministic tests and replays.
    """
    header = block.header
    _check_header_shape(block, params, parent_state, parent_hash)
    _check_timestamp(block, parent_state, now)
    expected_difficulty = _check_difficulty(block, params, parent_state)

    hash_hex = block_hash(header)
    if not seal_valid(hash_hex, header.seal_bits):
        _fail(
            BlockFailure.SEAL,
            f"hash {hash_hex} does not meet the {header.seal_bits}-bit seal target",
        )

    work = parent_state.copy()
    record: EpochRecord | None
    if header.height % params.epoch_length == 0:
        beacon = epoch_beacon(params, parent_state, header.epoch, parent_hash)
        record = work.open_epoch(header.epoch, beacon=beacon, height=header.height)
    else:
        record = work.epochs.get(header.epoch)
        if record is None:
            _fail(BlockFailure.EPOCH, f"epoch {header.epoch} was never opened")
    expected_beacon = record.beacon.hash if record.beacon is not None else ZERO_HASH
    if header.beacon_hash != expected_beacon:
        _fail(
            BlockFailure.BEACON,
            f"header beacon is {header.beacon_hash}, the epoch computes {expected_beacon}",
        )
    if header.roster_root != record.roster_root:
        _fail(
            BlockFailure.ROSTER_ROOT,
            f"header roster root is {header.roster_root}, the epoch computes {record.roster_root}",
        )

    _check_body(work, params, block)

    work.height = header.height
    work.tip_hash = hash_hex
    work.timestamps = [*work.timestamps, header.timestamp][-MAX_RECENT_TIMESTAMPS:]
    fees = apply_block_body(work, params, header, block.transactions, block.proofs, book=book)

    subsidy = block_subsidy(params, header.height, parent_state.minted)
    expected_value = subsidy + fees
    coinbase = block.coinbase
    assert coinbase is not None  # _check_body rejected the alternative
    if coinbase.payload.get("amount") != expected_value:
        _fail(
            BlockFailure.COINBASE,
            f"coinbase pays {coinbase.payload.get('amount')}, expected {expected_value} "
            f"(subsidy {subsidy} + fees {fees})",
        )
    work.credit_coinbase(header.miner, expected_value, header.height)

    if header.state_root != work.state_root():
        _fail(
            BlockFailure.STATE_ROOT,
            f"header state root is {header.state_root}, the block applies to {work.state_root()}",
        )

    parent_difficulty = parent_state.difficulty
    if parent_difficulty is not None and header.wins_required != parent_difficulty.wins_required:
        work.last_k_change_height = header.height
    work.difficulty = Difficulty(wins_required=header.wins_required, seal_bits=header.seal_bits)
    assert work.difficulty == expected_difficulty
    return ConnectedBlock(
        block=block,
        block_hash=hash_hex,
        work=work.difficulty.work,
        state=work,
    )


__all__ = [
    "HEADER_VERSION",
    "MAX_FUTURE_SECONDS",
    "BlockFailure",
    "ConnectedBlock",
    "apply_block_body",
    "block_hash",
    "block_size",
    "block_subsidy",
    "body_root",
    "bootstrap_end_height",
    "check_proof",
    "connect_block",
    "epoch_beacon",
    "header_payload",
    "merkle_root_of",
    "required_proofs",
]
