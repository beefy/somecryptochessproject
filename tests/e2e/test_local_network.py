"""A two-node local network over real sockets: mine, partition, heal.

The unit suite reaches the node through an in-process ASGI transport. This
test repeats the important path over actual TCP ports on loopback (with
uvicorn, the same server ``sccp node run`` uses), so a regression in the
transport, the lifespan or the JSON encoding cannot hide behind ASGI.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import httpx
import uvicorn

from sccp.chain.genesis import faucet_keypair
from sccp.chain.tx import txid
from sccp.config import DEVNET
from sccp.crypto.address import encode_address
from sccp.crypto.pq import hybrid_keypair_from_seed
from sccp.node.app import create_app
from sccp.node.p2p import GossipKind, sign_envelope
from sccp.node.service import HttpPeerClient, NodeService
from sccp.wallet.service import WalletService

FAUCET = faucet_keypair("devnet")
MINER = hybrid_keypair_from_seed(bytes([9]) * 32)
MINER_ADDRESS = encode_address(MINER.public_key_bytes, DEVNET)

START_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True, slots=True)
class LiveNode:
    """A node serving on a loopback port."""

    service: NodeService
    url: str


@contextmanager
def _serve(service: NodeService) -> Iterator[LiveNode]:
    """Run ``service`` under uvicorn on an ephemeral port until the block exits."""
    config = uvicorn.Config(
        create_app(service),
        host="127.0.0.1",
        port=0,
        log_level="warning",
        lifespan="on",
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="sccp-test-node", daemon=True)
    thread.start()
    deadline = time.monotonic() + START_TIMEOUT_SECONDS
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("the test node did not start")
        if not thread.is_alive():
            raise RuntimeError("the test node exited while starting")
        time.sleep(0.01)
    sockets = server.servers[0].sockets
    port = sockets[0].getsockname()[1]
    try:
        yield LiveNode(service=service, url=f"http://127.0.0.1:{port}")
    finally:
        server.should_exit = True
        thread.join(timeout=START_TIMEOUT_SECONDS)
        assert not thread.is_alive(), "the test node did not shut down"


def test_status_and_blocks_answer_over_tcp() -> None:
    service = NodeService.open(DEVNET)
    try:
        with _serve(service) as node, httpx.Client(base_url=node.url, timeout=10.0) as http:
            status = http.get("/v1/status")
            genesis = http.get("/v1/blocks/0")
            missing = http.get("/v1/blocks/999")
        assert status.status_code == 200
        assert status.json()["network"] == "devnet"
        assert status.json()["genesis_hash"] == service.genesis_hash
        assert genesis.json()["hash"] == service.genesis_hash
        assert missing.status_code == 404
    finally:
        service.close()


def test_two_nodes_converge_on_the_same_tip_after_a_partition(
    mine_block: Any,
) -> None:
    """A mines while B is offline; when B comes up it catches up, then A follows."""
    first = NodeService.open(DEVNET)
    second = NodeService.open(DEVNET)
    try:
        for _ in range(2):
            mine_block(first.chain, MINER)
        assert second.height == 0

        with _serve(first) as live_first:
            client = HttpPeerClient(live_first.url)
            try:
                caught_up = second.sync_from(client, url=live_first.url)
            finally:
                client.close()
        assert caught_up.adopted == 2
        assert second.tip_hash == first.tip_hash

        mine_block(second.chain, MINER)
        with _serve(second) as live_second:
            client = HttpPeerClient(live_second.url)
            try:
                healed = first.sync_from(client, url=live_second.url)
            finally:
                client.close()
        assert healed.adopted == 1
        assert first.tip_hash == second.tip_hash
        assert first.height == second.height == 3
    finally:
        first.close()
        second.close()


def test_a_signed_transaction_gossips_over_the_wire() -> None:
    service = NodeService.open(DEVNET)
    try:
        tx = WalletService(DEVNET, service.chain.state).build_transfer(FAUCET, MINER_ADDRESS, 5_000)
        envelope = sign_envelope(
            FAUCET,
            network="devnet",
            kind=GossipKind.TRANSACTION,
            payload={"transaction": tx.model_dump(mode="json", by_alias=True)},
        )
        with _serve(service) as node, httpx.Client(base_url=node.url, timeout=10.0) as http:
            announced = http.post("/v1/gossip", json=envelope.model_dump(mode="json"))
            pool = http.get("/v1/mempool")
        assert announced.status_code == 200, announced.text
        assert announced.json()["object_id"] == txid(tx)
        assert pool.json()["transactions"] == [txid(tx)]
    finally:
        service.close()
