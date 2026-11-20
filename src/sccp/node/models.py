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
    VariantName,
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
    minted: int = Field(default=0, ge=0)
    burned: int = Field(default=0, ge=0)
    """Native SCCP destroyed by timeout and witness slashes.

    The field is informational accounting: the burn is already committed by the
    reduced balances in ``state_root``. A state restored from a snapshot written
    before the counter existed reports zero until the burns that follow are
    applied.
    """
    minted_assets: dict[str, int] = Field(default_factory=dict)
    witnesses: int = Field(default=0, ge=0)
    """Active clock witnesses in the current epoch.

    A relay counts this against ``clock_witness_quorum`` before it pairs: a
    match played below the quorum can produce a flag that no bonded claim can
    settle. Nodes that predate the field report nothing, which a reader must
    treat as *unknown* rather than zero.
    """


class HtlcView(ProtocolModel):
    """One SCCP-side hash time-locked contract.

    ``asset`` is ``null`` for native SCCP and a 64-hex asset id for a variant
    token. ``status`` is the only mutable field: the id, hashlock, timelock and
    beneficiary are fixed when the lock is mined, and closing records the height
    that claimed or refunded it.
    """

    htlc_id: str
    network: str = Field(min_length=1)
    sender: str = Field(min_length=1)
    beneficiary: str = Field(min_length=1)
    hashlock: str
    timelock_height: int = Field(ge=0)
    amount: int = Field(gt=0)
    created_at_height: int = Field(ge=0)
    asset: str | None = None
    status: Literal["locked", "claimed", "refunded"] = "locked"
    closed_at_height: int | None = Field(default=None, ge=0)
    preimage: str | None = None


class HtlcList(ProtocolModel):
    """``GET /v1/htlcs``: the locks involving one account or one hashlock."""

    network: str = Field(min_length=1)
    htlcs: tuple[HtlcView, ...] = ()


class BridgeView(ProtocolModel):
    """``GET /v1/bridge``: the Ethereum environment this node is paired with.

    The document is advisory. A wallet still checks the chain id reported by its
    Ethereum provider and refuses to sign a lock when the two disagree.
    """

    network: str = Field(min_length=1)
    eth_chain_id: int = Field(ge=1)
    eth_chain_name: str = Field(min_length=1)
    eth_explorer_url: str | None = None
    eth_rpc_url: str | None = None
    eth_htlc_contract: str | None = None
    eth_confirmations: int = Field(ge=1)
    sccp_margin: int = Field(ge=1)
    configured: bool


class OrderView(ProtocolModel):
    """One open sell-SCCP order from ``GET /v1/orders``."""

    order_id: str
    network: str = Field(min_length=1)
    maker_sccp: str = Field(min_length=1)
    maker_eth: str = Field(min_length=1)
    give_asset: Literal["SCCP"] = "SCCP"
    give_amount: int = Field(gt=0)
    want_asset: Literal["ETH"] = "ETH"
    want_amount: str = Field(pattern=r"^[1-9][0-9]*$", max_length=78)
    min_sccp_timelock: int = Field(ge=0)
    expiry: int = Field(ge=0)
    nonce: int = Field(ge=0)
    public_key: str = Field(min_length=1)
    signature: str = Field(min_length=1)


class TakeView(ProtocolModel):
    """A taker's signed announcement that its ETH leg is locked."""

    take_id: str
    network: str = Field(min_length=1)
    order_id: str = Field(min_length=1)
    taker_sccp: str = Field(min_length=1)
    taker_eth: str = Field(min_length=1)
    hashlock: str
    eth_timelock: int = Field(ge=0)
    sccp_timelock_height: int = Field(ge=0)
    eth_tx_hash: str = Field(min_length=1)
    nonce: int = Field(ge=0)
    public_key: str = Field(min_length=1)
    signature: str = Field(min_length=1)


class OrderList(ProtocolModel):
    """``GET /v1/orders``: open orders, best price first."""

    network: str = Field(min_length=1)
    orders: tuple[OrderView, ...] = ()


class OrderDetail(ProtocolModel):
    """``GET /v1/orders/{order_id}``: an order and its single take, if any."""

    network: str = Field(min_length=1)
    order: OrderView
    take: TakeView | None = None


class OrderAccepted(ProtocolModel):
    """``POST /v1/orders``: the id of the admitted signed order."""

    network: str = Field(min_length=1)
    order_id: str
    accepted: bool = True


class TakeAccepted(ProtocolModel):
    """``POST /v1/orders/{order_id}/takes``: the id of the admitted take."""

    network: str = Field(min_length=1)
    take_id: str
    accepted: bool = True


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
    variant: str = "standard"
    assets: dict[str, int] = Field(default_factory=dict)
    immature_assets: dict[str, int] = Field(default_factory=dict)
    public_key: str | None = None
    banned_until: int = Field(default=0, ge=0)
    htlcs: tuple[HtlcView, ...] = ()


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


class GameView(ProtocolModel):
    """``GET /v1/games/{game_hash}``: proof status for one game."""

    game_hash: str
    network: str = Field(min_length=1)
    proof_id: str | None = None
    match_id: str | None = None
    winner: str | None = None
    variant: VariantName = "standard"
    height: int | None = Field(default=None, ge=0)
    in_mempool: bool = False


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
    variant: VariantName = "standard"
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
    variants: dict[str, int] = Field(default_factory=dict)
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
    variant: VariantName = "standard"
    height: int | None = Field(default=None, ge=0)
    in_mempool: bool = False
    low_ply: bool = False


class GameList(ProtocolModel):
    """``GET /v1/games``: pending proofs first, then newest settle first."""

    network: str = Field(min_length=1)
    games: tuple[GameSummary, ...] = ()


class PoolView(ProtocolModel):
    """One DEX pool as ``GET /v1/pools`` reports it.

    ``asset_a``/``asset_b`` are ``null`` for native SCCP and a 64-hex asset id
    otherwise. ``shares`` maps every LP address to its pool shares, which is what
    lets a wallet show its own position without a second request.
    """

    pool_id: str
    network: str = Field(min_length=1)
    asset_a: str | None = None
    asset_b: str | None = None
    reserve_a: int = Field(ge=0)
    reserve_b: int = Field(ge=0)
    total_shares: int = Field(ge=0)
    fee_bps: int = Field(ge=0, le=1_000)
    shares: dict[str, int] = Field(default_factory=dict)


class PoolList(ProtocolModel):
    """``GET /v1/pools``: every pool on the adopted branch."""

    network: str = Field(min_length=1)
    pools: tuple[PoolView, ...] = ()


__all__ = [
    "AccountView",
    "BlockIngest",
    "BlockList",
    "BlockSummary",
    "BlockView",
    "BridgeView",
    "DifficultyPoint",
    "DifficultyView",
    "EpochView",
    "FaucetRequest",
    "FaucetView",
    "GameList",
    "GameSummary",
    "GameView",
    "GossipResult",
    "HtlcList",
    "HtlcView",
    "MatchInfo",
    "MempoolStatus",
    "MempoolView",
    "MinerRatingDetail",
    "MinerRatingView",
    "NodeStatus",
    "OrderAccepted",
    "OrderDetail",
    "OrderList",
    "OrderView",
    "PeerView",
    "PoolList",
    "PoolView",
    "ProofAccepted",
    "RatingBucketView",
    "RatingDistribution",
    "RatingPointView",
    "RatingsView",
    "SyncReport",
    "TakeAccepted",
    "TakeView",
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
    variant: VariantName = "standard"
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
    variant: VariantName = "standard"
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
