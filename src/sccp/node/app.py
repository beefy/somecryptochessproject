"""The node HTTP API (PROTOCOL.md section 14).

The app is a thin, fully typed wrapper around :class:`sccp.node.service.NodeService`:
every route parses a request, calls one service method and returns a frozen
pydantic model. Errors are translated once, in handlers, so a route never
turns a ledger error into a 500 by accident.

Startup is deliberately loud: :func:`create_app` re-checks the network id and
the pinned genesis hash before accepting traffic, and the lifespan closes the
storage and peer clients on shutdown.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Final

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from sccp.bridge.orders import Order, Take
from sccp.errors import (
    FaucetDisabledError,
    NotFoundError,
    OrphanBlockError,
    RateLimitError,
    SccpError,
)
from sccp.node.models import (
    AccountView,
    BlockList,
    BlockView,
    BridgeView,
    DifficultyView,
    EpochView,
    FaucetRequest,
    FaucetView,
    GameList,
    GamePgn,
    GameView,
    GossipResult,
    HtlcList,
    HtlcView,
    MatchInfo,
    MempoolView,
    MinerRatingDetail,
    NodeStatus,
    OrderAccepted,
    OrderDetail,
    OrderList,
    PeerView,
    PoolList,
    PoolView,
    ProofAccepted,
    RatingsView,
    TakeAccepted,
    TransactionRecord,
    TxAccepted,
)
from sccp.node.p2p import GossipEnvelope
from sccp.node.service import NodeService
from sccp.protocol.models import Beacon, Block, Transaction, Transcript, WinProof
from sccp.version import __version__

WEB_DIST_ENV = "SCCP_WEB_DIST"
"""Environment variable pointing at a built explorer (a directory with ``index.html``)."""


def web_dist_root() -> Path | None:
    """The built explorer's directory, or ``None`` when nothing has been built.

    ``SCCP_WEB_DIST`` is an override, not a hint: an operator (or a container)
    that points it at a directory without an ``index.html`` gets no explorer
    rather than a stale bundle from somewhere else on disk. Without the variable
    the only candidate is ``src/web/dist`` next to a source checkout; an
    installed wheel carries no explorer, so ``None`` is the normal answer there
    and the node simply serves its JSON API alone.
    """
    configured = os.environ.get(WEB_DIST_ENV)
    if configured:
        directory = Path(configured)
        return directory if (directory / "index.html").is_file() else None
    fallback = Path(__file__).resolve().parents[2] / "web" / "dist"
    return fallback if (fallback / "index.html").is_file() else None


def _error_response(status_code: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"detail": message})


CONTRACT_IGNORED_KEYS = frozenset(
    {"description", "examples", "operationId", "summary", "tags", "title"}
)
"""Schema keys that may change without affecting a client.

Prose, generated ids and the Swagger group names organize the page a human
reads; dropping them keeps a documentation edit out of the pinned contract in
``tests/fixtures/openapi.json``, while routes, fields and status codes stay in it.
"""


def _normalise(value: Any) -> Any:
    """Recursively drop prose and generated ids from a JSON document."""
    if isinstance(value, dict):
        return {
            key: _normalise(item)
            for key, item in sorted(value.items())
            if key not in CONTRACT_IGNORED_KEYS
        }
    if isinstance(value, list):
        return [_normalise(item) for item in value]
    return value


def openapi_contract(app: FastAPI) -> dict[str, Any]:
    """The API schema projected onto the parts a client actually depends on.

    Prose, generated operation ids and Swagger group names are dropped so
    documentation edits do not churn the snapshot, and ``info`` is dropped because
    it carries the package version. Routes, parameters, request bodies, response
    codes and model fields all survive, so an accidental breaking change fails
    ``tests/unit/test_api.py`` until ``scripts/make_openapi_fixture.py`` is run
    on purpose.
    """
    schema = _normalise(app.openapi())
    assert isinstance(schema, dict)  # FastAPI always returns a mapping
    schema.pop("info", None)
    return schema


API_DESCRIPTION: Final = """The node's JSON API (PROTOCOL.md section 14.1).

Every response is a frozen pydantic model and every top-level body names its
network, so a wallet can refuse a node on the wrong one. `GET /v1/status` is the
health check. Errors are `{"detail": "..."}`: 400 for a rejected object, 404 for
an unknown id, 429 for a rate-limited faucet and 202 for a block whose parent is
unknown.

The wire formats behind these routes -- transaction payloads, the epoch beacon,
the block checklist -- are in the protocol document; this page documents the
routes, their parameters and their response models.
"""
"""What the Swagger UI landing page says about itself."""

OPENAPI_TAGS: Final[tuple[dict[str, str], ...]] = (
    {"name": "node", "description": "This node: identity, parameters, mempool and peers."},
    {
        "name": "chain",
        "description": "Blocks, epochs, beacons, difficulty and confirmed transactions.",
    },
    {"name": "ledger", "description": "Accounts, signed submissions and the testnet faucet."},
    {
        "name": "chess",
        "description": "Games, matches, transcripts and the derived miner ratings.",
    },
    {
        "name": "defi",
        "description": "Liquidity pools, the HTLC bridge and the off-consensus order book.",
    },
)
"""Sections of the Swagger UI, in display order. Prose only: not in the contract."""


def create_app(service: NodeService, *, web_root: Path | None = None) -> FastAPI:
    """Build the FastAPI app for one node service.

    ``web_root`` mounts a built explorer (``src/web/dist``) at ``/`` so the JSON
    API and the HTML client share an origin; without it the app serves the API
    alone and nothing about the contract changes.
    """
    service.check_startup()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            service.close()

    app = FastAPI(
        title=f"sccp node ({service.params.network_id})",
        version=__version__,
        description=API_DESCRIPTION,
        openapi_tags=list(OPENAPI_TAGS),
        lifespan=lifespan,
    )
    app.state.service = service

    @app.exception_handler(NotFoundError)
    async def _not_found(_: Request, exc: NotFoundError) -> JSONResponse:
        return _error_response(404, str(exc))

    @app.exception_handler(RateLimitError)
    async def _rate_limited(_: Request, exc: RateLimitError) -> JSONResponse:
        response = _error_response(429, str(exc))
        response.headers["Retry-After"] = str(exc.retry_after)
        return response

    @app.exception_handler(OrphanBlockError)
    async def _orphan(_: Request, exc: OrphanBlockError) -> JSONResponse:
        return _error_response(202, str(exc))

    @app.exception_handler(FaucetDisabledError)
    async def _faucet_disabled(_: Request, exc: FaucetDisabledError) -> JSONResponse:
        return _error_response(404, str(exc))

    @app.exception_handler(SccpError)
    async def _bad_request(_: Request, exc: SccpError) -> JSONResponse:
        return _error_response(400, str(exc))

    @app.get("/v1/status", response_model=NodeStatus, tags=["node"])
    def status() -> NodeStatus:
        """Return the node's network, height, tip hash and sync state.

        This is the cheapest route for a client or a load balancer to check that
        the node is on the expected network and is following the head of the
        chain. It never reads beyond the service's in-memory tip.
        """
        return service.status()

    @app.get("/v1/params", tags=["node"])
    def params() -> dict[str, object]:
        """Return the network parameters this node enforces.

        The document is exactly the validated :class:`~sccp.config.NetworkParams`
        for the node's network, so a client can derive fees, bond sizes and
        consensus constants without hard-coding them.
        """
        return service.params.model_dump(mode="json")

    @app.get("/v1/bridge", response_model=BridgeView, tags=["defi"])
    def bridge() -> BridgeView:
        """Return the bridge's Ethereum environment and confirmation policy.

        The view names the network, the configured RPC endpoint and HTLC contract
        (when an operator supplied them) and the confirmation count a claim
        waits for. A node without a configured bridge still answers, with those
        fields unset, instead of pretending the bridge exists.
        """
        return service.bridge_view()

    @app.get("/v1/orders", response_model=OrderList, tags=["defi"])
    def orders(
        maker_sccp: str | None = Query(default=None),
        give_asset: str | None = Query(default=None),
        want_asset: str | None = Query(default=None),
    ) -> OrderList:
        """List open off-consensus orders, optionally filtered by maker or asset.

        An empty string or omitted asset means native ZQWX. The list is a view
        of the node's current order book snapshot; it is not consensus data.
        """
        return service.order_list(
            maker_sccp=maker_sccp, give_asset=give_asset, want_asset=want_asset
        )

    @app.get("/v1/orders/{order_id}", response_model=OrderDetail, tags=["defi"])
    def order(order_id: str) -> OrderDetail:
        """Return one order by its 32-byte lowercase hex id.

        The detail includes the maker, the two sides, the remaining amount and
        the takes already recorded against it. An unknown or expired order is a
        ``404`` with the shared error envelope.
        """
        return service.order_detail(order_id)

    @app.post("/v1/orders", response_model=OrderAccepted, tags=["defi"])
    def submit_order(order: Order) -> OrderAccepted:
        """Admit a signed order to the off-consensus book.

        The order carries its maker's signature over the canonical body; the
        node re-verifies it, checks the maker can cover the give side and returns
        the stored order id. Nothing here touches the chain.
        """
        return service.submit_order(order)

    @app.post("/v1/orders/{order_id}/takes", response_model=TakeAccepted, tags=["defi"])
    def submit_take(order_id: str, take: Take) -> TakeAccepted:
        """Admit a signed take against the order named by the route.

        The take must name the same order id as the route and be signed by the
        taker. The node rejects a missing order with ``404`` and a mismatched or
        unfunded take as a validation error.
        """
        return service.submit_take(order_id, take)

    @app.get("/v1/htlcs", response_model=HtlcList, tags=["defi"])
    def htlcs(
        address: str | None = Query(default=None),
        hashlock: str | None = Query(default=None, pattern=r"^[0-9a-f]{64}$"),
    ) -> HtlcList:
        """List HTLCs, optionally filtered by participant or hashlock.

        ``address`` matches either side of the lock and ``hashlock`` must be the
        full 64-character lowercase hex digest. The filters combine with AND; no
        filter returns every HTLC the chain knows.
        """
        return service.htlcs(address=address, hashlock=hashlock)

    @app.get("/v1/htlcs/{htlc_id}", response_model=HtlcView, tags=["defi"])
    def htlc(htlc_id: str) -> HtlcView:
        """Return one HTLC by its 32-byte lowercase hex id.

        The view carries the lock, both parties, the hashlock, the timelock and
        whether the lock is still open, claimed or refunded. Unknown ids are a
        ``404``.
        """
        return service.htlc(htlc_id)

    @app.get("/v1/blocks", response_model=BlockList, tags=["chain"])
    def blocks(
        limit: int = Query(default=25, ge=1, le=200),
        before_height: int | None = Query(default=None, ge=0),
    ) -> BlockList:
        """List adopted blocks newest first, with a height cursor for paging.

        ``before_height`` returns only blocks strictly older than it, which is
        how the explorer walks the chain without loading it all. The response
        also reports the current tip height so a caller knows when it is done.
        """
        return service.blocks(limit=limit, before_height=before_height)

    @app.get("/v1/blocks/{reference}", response_model=BlockView, tags=["chain"])
    def block(reference: str) -> BlockView:
        """Return one block by height or by 32-byte lowercase hex hash.

        A numeric path segment is a height; anything else is treated as a hash.
        Unknown references are a ``404``. ``main_chain`` says whether the block
        is on the adopted branch, so an orphan or stale block is still readable.
        """
        return service.block(reference)

    @app.post("/v1/blocks", response_model=BlockView, tags=["chain"])
    def submit_block(block: Block) -> BlockView:
        """Validate and admit a signed block, returning the stored view.

        The node rebuilds the block from its transactions, checks the parent,
        proofs and state transition, then commits it. A block whose parent is not
        known yet is accepted into the orphan pool and answered with ``202``
        rather than treated as invalid.
        """
        ingested = service.ingest_block(block)
        if not ingested.accepted:
            raise OrphanBlockError("block is an orphan; parent unknown")
        return service.block(ingested.hash)

    @app.get("/v1/accounts/{address}", response_model=AccountView, tags=["ledger"])
    def account(address: str) -> AccountView:
        """Return one account's spendable, immature and bonded balances.

        The response includes the next ledger nonce, assets, the registered
        public key and the miner's registration and bond status. A malformed or
        foreign-network address is a ``400``; an address the chain has never seen
        is a zero-valued account rather than a ``404``.
        """
        return service.account(address)

    @app.get("/v1/pools", response_model=PoolList, tags=["defi"])
    def pools() -> PoolList:
        """List every AMM pool with its reserves, fee and LP shares.

        ``asset_a`` of ``null`` means native ZQWX. The values are the canonical
        pool state the DEX transactions act on, so a client can quote a swap
        from this response alone.
        """
        return service.pools()

    @app.get("/v1/pools/{pool_id}", response_model=PoolView, tags=["defi"])
    def pool(pool_id: str) -> PoolView:
        """Return one AMM pool by its 32-byte lowercase hex id.

        The view carries both reserves, the total LP supply, every holder's
        shares and the fee in basis points. An unknown pool id is a ``404``.
        """
        return service.pool(pool_id)

    @app.get("/v1/epochs/{epoch}", response_model=EpochView, tags=["chain"])
    def epoch(epoch: int) -> EpochView:
        """Return one epoch's availability commitments, reveals and roster.

        ``matchable`` says whether the epoch has a complete roster the relay can
        pair from. An epoch the chain has not opened is a ``404``.
        """
        return service.epoch(epoch)

    @app.get("/v1/epochs/{epoch}/beacon", response_model=Beacon, tags=["chain"])
    def epoch_beacon(epoch: int) -> Beacon:
        """Return the signed random beacon that seeds one epoch's pairings.

        The beacon is the chain's committed randomness for the epoch, not a
        freshly generated value; an epoch without a beacon is a ``404``.
        """
        return service.beacon(epoch)

    @app.get("/v1/difficulty", response_model=DifficultyView, tags=["chain"])
    def difficulty() -> DifficultyView:
        """Return the current difficulty target and the retarget history.

        The history is what the consensus retarget reads, so a miner can check
        that its local target matches the node's before submitting a block.
        """
        return service.difficulty()

    @app.get("/v1/ratings", response_model=RatingsView, tags=["chess"])
    def ratings(limit: int = Query(default=100, ge=1, le=1000)) -> RatingsView:
        """List miner ratings, strongest first, capped by ``limit``.

        Ratings are derived from proven games on the adopted chain, not from
        relay claims, so the list is deterministic for a given tip.
        """
        return service.ratings(limit=limit)

    @app.get("/v1/ratings/{address}", response_model=MinerRatingDetail, tags=["chess"])
    def miner_rating(address: str) -> MinerRatingDetail:
        """Return one miner's rating, games played and per-opponent record.

        An address that has never proven a game is a ``404``: an unrated miner
        has no rating to report. A malformed or foreign-network address is a
        ``400`` before any lookup.
        """
        return service.miner_rating(address)

    @app.get("/v1/games", response_model=GameList, tags=["chess"])
    def games(
        limit: int = Query(default=25, ge=1, le=200),
        before_height: int | None = Query(default=None, ge=0),
        before_game_hash: str | None = Query(default=None, min_length=1),
    ) -> GameList:
        """List proven games newest first with a paging cursor.

        ``before_height`` and ``before_game_hash`` page through games at the same
        block tied on height, so an explorer never skips or repeats a game at a
        boundary. Only games with an on-chain proof appear here.
        """
        return service.games(
            limit=limit,
            before_height=before_height,
            before_game_hash=before_game_hash,
        )

    @app.get("/v1/games/{game_hash}", response_model=GameView, tags=["chess"])
    def game(game_hash: str) -> GameView:
        """Return one proven game by its 32-byte lowercase hex hash.

        The view names both miners, the result, the block that carries the proof
        and the transcript digest. Unknown hashes are a ``404``.
        """
        return service.game(game_hash)

    @app.get("/v1/games/{game_hash}/pgn", response_model=GamePgn, tags=["chess"])
    def game_pgn(game_hash: str) -> GamePgn:
        """Return one proven game as a PGN document.

        The PGN is rebuilt from the certified transcript, so it is the same
        record the proofs cover rather than a client-supplied string. Unknown
        hashes are a ``404``.
        """
        return service.game_pgn(game_hash)

    @app.get("/v1/matches/{match_id}", response_model=MatchInfo, tags=["chess"])
    def match(match_id: str) -> MatchInfo:
        """Return a relay match visible to this node, by match id.

        The node keeps matches it was told about through gossip or its own
        sessions; an id it has never seen is a ``404``. This is not consensus
        data and may lag the relay.
        """
        return service.match(match_id)

    @app.get("/v1/matches/{match_id}/transcript", response_model=Transcript, tags=["chess"])
    def transcript(match_id: str) -> Transcript:
        """Return the certified transcript for one match id.

        The transcript is the co-signed move record a proof is built from; a
        match without one is a ``404``. Unknown fields are omitted by the model,
        so the response is exactly the wire transcript.
        """
        return service.transcript(match_id)

    @app.get("/v1/faucet", tags=["ledger"])
    def faucet_status() -> dict[str, object]:
        """Return the faucet's grant amount, cooldown and remaining budget.

        A node started without a faucet answers ``404`` rather than inventing an
        empty status, so a client can tell "disabled" from "out of coins".
        """
        if service.faucet is None:
            raise FaucetDisabledError("the faucet is disabled")
        return service.faucet.status()

    @app.post("/v1/faucet", response_model=FaucetView, tags=["ledger"])
    def faucet(request: FaucetRequest, http_request: Request) -> FaucetView:
        """Grant testnet coins to an address, subject to the faucet policy.

        One grant per address per cooldown window; a repeat is a ``429`` with a
        ``Retry-After`` header. A disabled faucet is a ``404`` and an unfunded or
        malformed request a ``400``.
        """
        client = http_request.client.host if http_request.client is not None else "unknown"
        return service.faucet_request(request.address, client=client, amount=request.amount)

    @app.get("/v1/mempool", response_model=MempoolView, tags=["node"])
    def mempool() -> MempoolView:
        """Return the transactions and proofs waiting for the next block.

        The view includes each entry's fee and size so a client can estimate
        inclusion; it is a snapshot of this node's mempool, not a network-wide
        guarantee.
        """
        return service.mempool_view()

    @app.get("/v1/transactions/{txid}", response_model=TransactionRecord, tags=["chain"])
    def transaction(txid: str) -> TransactionRecord:
        """Return one confirmed transaction by its 32-byte hex id.

        The record includes the block that carries it and the canonical body, so
        a wallet can reconcile its history against the chain. Unknown ids are a
        ``404``; a mempool-only transaction is not found here.
        """
        return service.transaction(txid)

    @app.post("/v1/tx", response_model=TxAccepted, tags=["ledger"])
    def submit_tx(tx: Transaction) -> TxAccepted:
        """Validate a signed transaction and admit it to the mempool.

        The node checks the signature, nonce, balance and ledger rules before
        returning the txid. A consensus-invalid transaction is a ``400``; one
        that is valid but not yet mined stays in the mempool.
        """
        return service.submit_transaction(tx)

    @app.post("/v1/proofs", response_model=ProofAccepted, tags=["ledger"])
    def submit_proof(proof: WinProof) -> ProofAccepted:
        """Validate a game proof and admit it to the mempool.

        A proof is accepted only when the certified transcript replays to the
        claimed result and both seats signed it; otherwise the failure is a
        ``400`` with the reason.
        """
        return service.announce_proof(proof)

    @app.post("/v1/gossip", response_model=GossipResult, tags=["node"])
    def gossip(envelope: GossipEnvelope) -> GossipResult:
        """Accept one signed gossip envelope from a peer.

        The node verifies the envelope's signature and content id, deduplicates
        it and fans it out to a subset of peers. A malformed envelope is a
        ``400``; the response reports whether it was new or already seen.
        """
        return service.handle_gossip(envelope)

    @app.get("/v1/peers", response_model=list[PeerView], tags=["node"])
    def peers() -> tuple[PeerView, ...]:
        """List known gossip peers with their health and ban state.

        A banned peer stays listed with its failure count and ban expiry so an
        operator can see why the node stopped dialing it.
        """
        return service.peers_view()

    if web_root is not None:
        # Mounted last: Starlette matches routes in declaration order, so the
        # JSON API keeps winning every path it owns.
        app.mount("/", StaticFiles(directory=web_root, html=True), name="web")

    return app


__all__ = [
    "CONTRACT_IGNORED_KEYS",
    "WEB_DIST_ENV",
    "create_app",
    "openapi_contract",
    "web_dist_root",
]
