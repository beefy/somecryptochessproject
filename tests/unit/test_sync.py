"""Pull-sync: range planning, block fetching and partition healing.

The two-node convergence test uses real blocks through an in-process ASGI peer;
``tests/e2e/test_local_network.py`` repeats it over actual sockets.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from sccp.config import DEVNET, TESTNET
from sccp.crypto.address import encode_address
from sccp.crypto.keys import keypair_from_seed
from sccp.errors import GossipError
from sccp.node.app import create_app
from sccp.node.p2p import fetch_blocks, plan_sync
from sccp.node.service import HttpPeerClient, NodeService, sync_peers
from sccp.transport import client_for

MINER = keypair_from_seed(bytes([9]) * 32)
MINER_ADDRESS = encode_address(MINER.public_key_bytes, DEVNET)


def _node(network: Any = DEVNET) -> NodeService:
    return NodeService.open(network)


def _peer_client(service: NodeService, url: str = "http://peer") -> HttpPeerClient:
    return HttpPeerClient(url, client=client_for(create_app(service)))


class _StubPeerClient:
    def __init__(self, *, status: dict[str, object], blocks: dict[str, object]) -> None:
        self._status = status
        self._blocks = blocks

    def status(self) -> dict[str, object]:
        return self._status

    def block(self, reference: str) -> dict[str, object]:
        return self._blocks[reference]  # type: ignore[return-value]


class _StubPeer:
    def __init__(self, blocks: dict[str, Any]) -> None:
        self.blocks = blocks

    def status(self) -> dict[str, object]:
        return {}

    def block(self, reference: str) -> dict[str, object]:
        return self.blocks[reference]


def test_plan_sync_covers_caught_up_and_clamped_ranges() -> None:
    assert plan_sync(5, 5) == (6, 5)
    assert plan_sync(5, 4) == (6, 5)
    assert plan_sync(0, 3) == (1, 3)
    assert plan_sync(0, 10, max_blocks=4) == (1, 4)


def test_fetch_blocks_accepts_wrapped_and_bare_documents(
    devnet_chain: Any, mine_block: Any
) -> None:
    miner = keypair_from_seed(bytes([3]) * 32)
    first = mine_block(devnet_chain, miner)
    second = mine_block(devnet_chain, miner)
    peer = _StubPeer(
        {"1": {"block": first.model_dump(mode="json")}, "2": second.model_dump(mode="json")}
    )
    blocks = fetch_blocks(peer, start_height=1, end_height=2)
    assert [block.header.height for block in blocks] == [1, 2]


def test_fetch_blocks_rejects_wrong_heights_and_malformed_documents(
    devnet_chain: Any, mine_block: Any
) -> None:
    miner = keypair_from_seed(bytes([3]) * 32)
    block = mine_block(devnet_chain, miner)
    wrong = _StubPeer({"2": {"block": block.model_dump(mode="json")}})
    with pytest.raises(GossipError, match="returned height 1"):
        fetch_blocks(wrong, start_height=2, end_height=2)
    malformed = _StubPeer({"1": {"block": {"nope": True}}})
    with pytest.raises(GossipError, match="malformed block"):
        fetch_blocks(malformed, start_height=1, end_height=1)


def test_sync_from_pulls_missing_blocks(devnet_chain: Any, mine_block: Any) -> None:
    source = _node()
    for _ in range(3):
        mine_block(source.chain, MINER)
    source_http = client_for(create_app(source))
    target = _node()
    with source_http:
        peer = HttpPeerClient("http://source", client=source_http)
        report = target.sync_from(peer, url="http://source")
        again = target.sync_from(peer, url="http://source")
    assert report.requested == 3
    assert report.adopted == 3
    assert report.height == source.height
    assert report.tip_hash == source.tip_hash
    assert target.tip_hash == source.tip_hash
    assert again.requested == 0 and again.adopted == 0
    assert target.peers.get("http://source") is not None


def test_sync_from_refuses_a_peer_on_another_network() -> None:
    target = _node()
    other = _node(TESTNET)
    with client_for(create_app(other)) as http:
        peer = HttpPeerClient("http://other", client=http)
        with pytest.raises(GossipError, match="testnet"):
            target.sync_from(peer, url="http://other")


def test_sync_from_penalises_a_peer_that_serves_garbage() -> None:
    target = _node()
    target.max_sync_blocks = 1
    peer = _StubPeerClient(status={"network": "devnet", "height": 4}, blocks={"1": {}})
    with pytest.raises(GossipError, match="malformed block"):
        target.sync_from(peer, url="http://bad")
    record = target.peers.get("http://bad")
    assert record is not None and record.failures == 1


def test_sync_from_counts_orphans(devnet_chain: Any, mine_block: Any) -> None:
    source = _node()
    mine_block(source.chain, MINER, timestamp=1_700_000_100)
    orphan = mine_block(source.chain, MINER, timestamp=1_700_000_101)
    service = _node()
    mine_block(service.chain, MINER)
    peer = _StubPeerClient(
        status={"network": "devnet", "height": 2},
        blocks={"2": {"block": orphan.model_dump(mode="json")}},
    )
    report = service.sync_from(peer, url="http://orphan")
    assert report.adopted == 0
    assert report.orphaned == 1


def test_http_peer_client_turns_errors_into_gossip_errors() -> None:
    service = _node()
    with client_for(create_app(service)) as http:
        peer = HttpPeerClient("http://node", client=http)
        assert peer.status()["network"] == "devnet"
        with pytest.raises(GossipError, match="cannot fetch"):
            peer.block("999")


def test_a_partitioned_node_converges_after_it_heals(devnet_chain: Any, mine_block: Any) -> None:
    """Node A mines alone, then B catches up and A adopts B's newer block."""
    first = _node()
    second = _node()
    for _ in range(2):
        mine_block(first.chain, MINER)
    assert second.height == 0

    with client_for(create_app(first)) as first_http:
        report = second.sync_from(
            HttpPeerClient("http://first", client=first_http), url="http://first"
        )
    assert report.adopted == 2
    assert second.tip_hash == first.tip_hash

    mine_block(second.chain, MINER)
    with client_for(create_app(second)) as second_http:
        healed = first.sync_from(
            HttpPeerClient("http://second", client=second_http), url="http://second"
        )
    assert healed.adopted == 1
    assert first.tip_hash == second.tip_hash
    assert first.height == second.height == 3


def _refuse(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("connection refused", request=request)


def test_sync_peers_skips_peers_that_are_down(devnet_chain: Any, mine_block: Any) -> None:
    source = _node()
    mine_block(source.chain, MINER)
    target = _node()

    def factory(url: str) -> HttpPeerClient:
        if url == "http://down":
            transport = httpx.MockTransport(_refuse)
            return HttpPeerClient(url, client=httpx.Client(transport=transport))
        return _peer_client(source, url)

    reports = sync_peers(target, ("http://down", "http://up"), client_factory=factory)
    assert [report.peer for report in reports] == ["http://up"]
    assert reports[0].adopted == 1
    assert target.height == 1
    down = target.peers.get("http://down")
    assert down is not None and down.failures == 1
