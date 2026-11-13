"""Response models for the node API (PROTOCOL.md section 14).

Every response is a frozen pydantic model with ``extra="forbid"``, so the
OpenAPI schema doubles as a contract and an unexpected field fails in tests
rather than silently reaching a client. All responses carry the network id
where the protocol asks for it, which is what lets a wallet refuse to talk to a
node on the wrong network.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from sccp.protocol.models import (
    AddressText,
    Beacon,
    Block,
    Color,
    MatchCertificate,
    ProtocolModel,
    ResultValue,
    RosterMember,
    TerminationValue,
    Transcript,
    TxType,
)


class MempoolStatus(ProtocolModel):
    """How many transactions and proofs are waiting for a block."""

    transactions: int = Field(ge=0)
    proofs: int = Field(ge=0)


class NodeStatus(ProtocolModel):
    """``GET /v1/status``: height, tip, peers and pool sizes."""

    network: str = Field(min_length=1)
    protocol_version: int = Field(ge=1)
    height: int = Field(ge=0)
    tip_hash: str
    genesis_hash: str
    peers: int = Field(ge=0)
    syncing: bool
    mempool: MempoolStatus


class AccountView(ProtocolModel):
    """``GET /v1/accounts/{address}``: everything the ledger knows."""

    network: str = Field(min_length=1)
    address: str = Field(min_length=1)
    balance: int = Field(ge=0)
    immature: int = Field(ge=0)
    bonded: int = Field(ge=0)
    total: int = Field(ge=0)
    nonce: int = Field(ge=0)
    registered_until_epoch: int | None = Field(default=None, ge=0)
    endpoint: str | None = None
    engine: str | None = None
    public_key: str | None = None
    banned_until: int = Field(default=0, ge=0)


class EpochView(ProtocolModel):
    """``GET /v1/epochs/{e}``: beacon, roster and reveals for one epoch."""

    epoch: int = Field(ge=0)
    network: str = Field(min_length=1)
    beacon: Beacon | None = None
    roster_root: str
    roster: dict[str, RosterMember] = Field(default_factory=dict)
    availability: dict[str, str] = Field(default_factory=dict)
    quotas: dict[str, int] = Field(default_factory=dict)
    matchable: bool
    ended_at_height: int | None = Field(default=None, ge=0)


class BlockView(ProtocolModel):
    """``GET /v1/blocks/{height_or_hash}``: the block plus its position."""

    network: str = Field(min_length=1)
    hash: str
    parent_hash: str
    height: int = Field(ge=0)
    chain_work: int = Field(ge=0)
    main_chain: bool
    block: Block


class DifficultyPoint(ProtocolModel):
    """One entry of the retarget history."""

    height: int = Field(ge=0)
    wins_required: int = Field(ge=1)
    seal_bits: int = Field(ge=0, le=32)


class MempoolView(ProtocolModel):
    """``GET /v1/mempool``: ids only, never full bodies."""

    network: str = Field(min_length=1)
    transactions: tuple[str, ...] = ()
    proofs: tuple[str, ...] = ()


class ChallengeView(ProtocolModel):
    """``GET /v1/challenges``: one bonded quality challenge."""

    challenge_id: str
    network: str = Field(min_length=1)
    proof_id: str
    game_hash: str
    challenger: str
    miner: str
    reason: str
    evidence: str
    bond: int = Field(ge=1)
    opened_height: int = Field(ge=0)
    deadline_height: int = Field(ge=0)
    status: Literal["open", "upheld", "rejected"]
    resolved_height: int | None = Field(default=None, ge=0)
    panel: tuple[str, ...] = ()


class ChallengeList(ProtocolModel):
    """``GET /v1/challenges``: the challenges this node knows about."""

    network: str = Field(min_length=1)
    challenges: tuple[ChallengeView, ...] = ()


class GameView(ProtocolModel):
    """``GET /v1/games/{game_hash}``: proof and challenge status."""

    game_hash: str
    network: str = Field(min_length=1)
    proof_id: str | None = None
    match_id: str | None = None
    winner: str | None = None
    height: int | None = Field(default=None, ge=0)
    in_mempool: bool = False
    challenge_status: Literal["none", "open", "upheld", "rejected"] = "none"
    quality_verdict: Literal["pass", "fail"] | None = None
    quality_trace_hash: str | None = None


class GamePgn(ProtocolModel):
    """``GET /v1/games/{game_hash}/pgn``: the move text a proof carries.

    The game page needs the PGN even when this node never cached a transcript
    for the match, which is the common case for a node that only followed the
    chain: the proof body in its block has it, so the body is the source.
    """

    game_hash: str
    network: str = Field(min_length=1)
    pgn: str = Field(min_length=1)


class MatchInfo(ProtocolModel):
    """``GET /v1/matches/{match_id}``: transcript metadata known to the node."""

    match_id: str
    network: str = Field(min_length=1)
    epoch: int = Field(ge=0)
    round: int = Field(ge=0)
    seats: tuple[str, str]
    certificate: MatchCertificate | None = None
    transcript: Transcript | None = None


class TxAccepted(ProtocolModel):
    """``POST /v1/tx``: the txid the node assigned to a signed transaction."""

    network: str = Field(min_length=1)
    txid: str
    accepted: bool = True


class ProofAccepted(ProtocolModel):
    """``POST /v1/proofs``: the proof id the node accepted."""

    network: str = Field(min_length=1)
    proof_id: str
    accepted: bool = True


class PeerView(ProtocolModel):
    """``GET /v1/peers``: one known peer and its abuse score."""

    network: str = Field(min_length=1)
    url: str = Field(min_length=1)
    address: str | None = None
    height: int = Field(default=0, ge=0)
    failures: int = Field(default=0, ge=0)
    banned_until: int = Field(default=0, ge=0)
    last_seen: int = Field(default=0, ge=0)


class GossipResult(ProtocolModel):
    """``POST /v1/gossip``: what the node did with an envelope."""

    network: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    object_id: str
    duplicate: bool
    fanout: tuple[str, ...] = ()


class BlockIngest(ProtocolModel):
    """The result of handing one block to the node."""

    hash: str
    height: int = Field(ge=0)
    accepted: bool
    duplicate: bool = False
    reorged: bool = False

    @property
    def changed(self) -> bool:
        """Whether this block moved the node's state."""
        return self.accepted and not self.duplicate


class SyncReport(ProtocolModel):
    """The outcome of one pull-sync against a peer."""

    peer: str = Field(min_length=1)
    start_height: int = Field(ge=0)
    requested: int = Field(ge=0)
    adopted: int = Field(ge=0)
    orphaned: int = Field(ge=0)
    height: int = Field(ge=0)
    tip_hash: str


class TransactionRecord(ProtocolModel):
    """One transaction as returned by ``GET /v1/transactions/{txid}``."""

    txid: str
    network: str = Field(min_length=1)
    type: TxType
    height: int | None = Field(default=None, ge=0)
    in_mempool: bool = False
    block_hash: str | None = None


class FaucetRequest(ProtocolModel):
    """``POST /v1/faucet``: the recipient address and an optional amount in coins."""

    address: str = Field(min_length=1)
    amount: int | None = Field(default=None, ge=1)


class FaucetView(ProtocolModel):
    """A dispensed faucet grant."""

    network: str = Field(min_length=1)
    address: str = Field(min_length=1)
    amount: int = Field(ge=1)
    txid: str = Field(min_length=1)


class BlockSummary(ProtocolModel):
    """One row of ``GET /v1/blocks``: enough to list a block, not to verify it.

    The full block is one request away at ``GET /v1/blocks/{height}``; a list
    response stays small so an explorer can page the tip without shipping every
    proof's PGN.
    """

    hash: str
    parent_hash: str
    height: int = Field(ge=0)
    timestamp: int = Field(ge=0)
    epoch: int = Field(ge=0)
    miner: AddressText
    transactions: int = Field(ge=0)
    proofs: int = Field(ge=0)
    wins_required: int = Field(ge=1)
    seal_bits: int = Field(ge=0, le=32)


class BlockList(ProtocolModel):
    """``GET /v1/blocks``: the newest blocks on the adopted branch, tip first."""

    network: str = Field(min_length=1)
    tip_height: int = Field(ge=0)
    blocks: tuple[BlockSummary, ...] = ()


class GameSummary(ProtocolModel):
    """One row of ``GET /v1/games``: a settled proof, or one waiting for a block.

    ``height`` is the block the proof settled in and is ``None`` for a proof
    that is still in the mempool (``in_mempool``). The result fields are filled
    from the proof itself, so they are ``None`` only when this node no longer
    holds the block body (a pruned or snapshotted chain).
    """

    game_hash: str
    proof_id: str
    match_id: str | None = None
    epoch: int | None = Field(default=None, ge=0)
    round: int | None = Field(default=None, ge=0)
    winner: AddressText
    loser: AddressText | None = None
    winner_seat: Color | None = None
    result: ResultValue | None = None
    termination: TerminationValue | None = None
    plies: int | None = Field(default=None, ge=1)
    height: int | None = Field(default=None, ge=0)
    in_mempool: bool = False
    low_ply: bool = False
    challenge_status: Literal["none", "open", "upheld", "rejected"] = "none"
    quality_verdict: Literal["pass", "fail"] | None = None


class GameList(ProtocolModel):
    """``GET /v1/games``: pending proofs first, then newest settle first."""

    network: str = Field(min_length=1)
    games: tuple[GameSummary, ...] = ()


__all__ = [
    "AccountView",
    "BlockIngest",
    "BlockList",
    "BlockSummary",
    "BlockView",
    "DifficultyPoint",
    "DifficultyView",
    "EpochView",
    "FaucetRequest",
    "FaucetView",
    "GameList",
    "GameSummary",
    "GameView",
    "GossipResult",
    "MatchInfo",
    "MempoolStatus",
    "MempoolView",
    "MinerRatingDetail",
    "MinerRatingView",
    "NodeStatus",
    "PeerView",
    "ProofAccepted",
    "RatingBucketView",
    "RatingDistribution",
    "RatingPointView",
    "RatingsView",
    "SyncReport",
    "TransactionRecord",
    "TxAccepted",
]


class DifficultyView(ProtocolModel):
    """``GET /v1/difficulty``: the current target and where it came from."""

    network: str = Field(min_length=1)
    height: int = Field(ge=0)
    wins_required: int = Field(ge=1)
    seal_bits: int = Field(ge=0, le=32)
    retarget_interval: int = Field(ge=1)
    retarget_enabled: bool
    history: tuple[DifficultyPoint, ...] = ()


class RatingPointView(ProtocolModel):
    """One period of a miner's rating history (``GET /v1/ratings/{address}``).

    ``epoch`` is the block epoch the period covers and ``height`` the last block
    that settled a game for this miner in it. A trailing point at the tip epoch
    carries the miner's current rating with an inflated deviation, which is how
    an idle miner's growing uncertainty appears on the chart.
    """

    epoch: int = Field(ge=0)
    height: int = Field(ge=0)
    rating: float
    deviation: float = Field(gt=0)
    volatility: float = Field(gt=0)
    games: int = Field(ge=0)
    wins: int = Field(ge=0)
    draws: int = Field(ge=0)
    losses: int = Field(ge=0)


class MinerRatingView(ProtocolModel):
    """One row of ``GET /v1/ratings``: a rated miner and its standing."""

    address: AddressText
    rank: int = Field(ge=1)
    rating: float
    deviation: float = Field(gt=0)
    volatility: float = Field(gt=0)
    games: int = Field(ge=0)
    wins: int = Field(ge=0)
    draws: int = Field(ge=0)
    losses: int = Field(ge=0)
    provisional: bool
    last_epoch: int = Field(ge=0)
    last_height: int = Field(ge=0)


class MinerRatingDetail(ProtocolModel):
    """``GET /v1/ratings/{address}``: one miner's rating and its whole history.

    An address that has never settled a game gets the prior (1500, deviation 350),
    ``games: 0`` and an empty history rather than a 404: the rating of a fresh
    registration is a real value, not an error.
    """

    network: str = Field(min_length=1)
    address: str = Field(min_length=1)
    rank: int | None = Field(default=None, ge=1)
    rating: float
    deviation: float = Field(gt=0)
    volatility: float = Field(gt=0)
    games: int = Field(ge=0)
    wins: int = Field(ge=0)
    draws: int = Field(ge=0)
    losses: int = Field(ge=0)
    provisional: bool
    last_epoch: int | None = Field(default=None, ge=0)
    last_height: int | None = Field(default=None, ge=0)
    history: tuple[RatingPointView, ...] = ()


class RatingBucketView(ProtocolModel):
    """One bar of the fixed-width rating histogram."""

    label: str = Field(min_length=1)
    low: int | None = None
    high: int | None = None
    count: int = Field(ge=0)


class RatingDistribution(ProtocolModel):
    """The distribution of current ratings across every rated miner."""

    buckets: tuple[RatingBucketView, ...] = ()
    rated: int = Field(ge=0)
    mean: float
    median: float


class RatingsView(ProtocolModel):
    """``GET /v1/ratings``: the derived leaderboard and its distribution.

    ``miners`` is every rated miner, strongest first, truncated by ``?limit=``.
    The distribution always covers *all* miners, not just the returned rows, so
    a leaderboard page still shows the shape of the whole field.
    """

    network: str = Field(min_length=1)
    height: int = Field(ge=0)
    tip_hash: str
    algorithm: Literal["glicko-2"] = "glicko-2"
    rating_period: Literal["epoch"] = "epoch"
    rated_epochs: int = Field(ge=0)
    games: int = Field(ge=0)
    miners: tuple[MinerRatingView, ...] = ()
    distribution: RatingDistribution
