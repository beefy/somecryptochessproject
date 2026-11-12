"""The node HTTP API (plan section 5.12).

The app is a thin, fully typed wrapper around :class:`sccp.node.service.NodeService`:
every route parses a request, calls one service method and returns a frozen
pydantic model. Errors are translated once, in handlers, so a route never
turns a ledger error into a 500 by accident.

Startup is deliberately loud: :func:`create_app` re-checks the network id and
the pinned genesis hash before accepting traffic, and the lifespan closes the
storage and peer clients on shutdown.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from sccp.errors import NotFoundError, SccpError
from sccp.node.models import (
    AccountView,
    BlockView,
    DifficultyView,
    EpochView,
    GameView,
    GossipResult,
    MatchInfo,
    MempoolView,
    NodeStatus,
    PeerView,
    ProofAccepted,
    TransactionRecord,
    TxAccepted,
)
from sccp.node.p2p import GossipEnvelope
from sccp.node.service import NodeService
from sccp.protocol.models import Beacon, Block, Transaction, Transcript, WinProof
from sccp.version import __version__


def _error_response(status_code: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"detail": message})


def create_app(service: NodeService) -> FastAPI:
    """Build the FastAPI app for one node service."""
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
        lifespan=lifespan,
    )
    app.state.service = service

    @app.exception_handler(NotFoundError)
    async def _not_found(_: Request, exc: NotFoundError) -> JSONResponse:
        return _error_response(404, str(exc))

    @app.exception_handler(SccpError)
    async def _bad_request(_: Request, exc: SccpError) -> JSONResponse:
        return _error_response(400, str(exc))

    @app.get("/v1/status", response_model=NodeStatus)
    def status() -> NodeStatus:
        return service.status()

    @app.get("/v1/params")
    def params() -> dict[str, object]:
        return service.params.model_dump(mode="json")

    @app.get("/v1/blocks/{reference}", response_model=BlockView)
    def block(reference: str) -> BlockView:
        return service.block(reference)

    @app.post("/v1/blocks", response_model=BlockView)
    def submit_block(block: Block) -> BlockView:
        ingested = service.ingest_block(block)
        if not ingested.accepted:
            raise HTTPException(status_code=202, detail="block is an orphan; parent unknown")
        return service.block(ingested.hash)

    @app.get("/v1/accounts/{address}", response_model=AccountView)
    def account(address: str) -> AccountView:
        return service.account(address)

    @app.get("/v1/epochs/{epoch}", response_model=EpochView)
    def epoch(epoch: int) -> EpochView:
        return service.epoch(epoch)

    @app.get("/v1/epochs/{epoch}/beacon", response_model=Beacon)
    def epoch_beacon(epoch: int) -> Beacon:
        return service.beacon(epoch)

    @app.get("/v1/difficulty", response_model=DifficultyView)
    def difficulty() -> DifficultyView:
        return service.difficulty()

    @app.get("/v1/games/{game_hash}", response_model=GameView)
    def game(game_hash: str) -> GameView:
        return service.game(game_hash)

    @app.get("/v1/matches/{match_id}", response_model=MatchInfo)
    def match(match_id: str) -> MatchInfo:
        return service.match(match_id)

    @app.get("/v1/matches/{match_id}/transcript", response_model=Transcript)
    def transcript(match_id: str) -> Transcript:
        return service.transcript(match_id)

    @app.get("/v1/mempool", response_model=MempoolView)
    def mempool() -> MempoolView:
        return service.mempool_view()

    @app.get("/v1/transactions/{txid}", response_model=TransactionRecord)
    def transaction(txid: str) -> TransactionRecord:
        return service.transaction(txid)

    @app.post("/v1/tx", response_model=TxAccepted)
    def submit_tx(tx: Transaction) -> TxAccepted:
        return service.submit_transaction(tx)

    @app.post("/v1/proofs", response_model=ProofAccepted)
    def submit_proof(proof: WinProof) -> ProofAccepted:
        return service.announce_proof(proof)

    @app.get("/v1/challenges")
    def challenges() -> Mapping[str, object]:
        # Quality challenges arrive with M5; the endpoint exists so clients can
        # poll it without special-casing an older node.
        return {"network": service.params.network_id, "challenges": []}

    @app.post("/v1/gossip", response_model=GossipResult)
    def gossip(envelope: GossipEnvelope) -> GossipResult:
        return service.handle_gossip(envelope)

    @app.get("/v1/peers", response_model=list[PeerView])
    def peers() -> tuple[PeerView, ...]:
        return service.peers_view()

    return app


__all__ = ["create_app"]
