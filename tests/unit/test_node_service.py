"""The node service and its HTTP API, exercised in-process (M4)."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from sccp.chain.genesis import faucet_keypair
from sccp.chain.tx import txid
from sccp.config import DEVNET, TESTNET
from sccp.crypto.address import encode_address
from sccp.crypto.keys import keypair_from_seed
from sccp.errors import GossipError, MempoolError, ProtocolError
from sccp.node.app import create_app
from sccp.node.app import create_app as create_node_app
from sccp.node.p2p import GossipKind, sign_envelope
from sccp.node.service import HttpPeerClient, NodeService
from sccp.transport import client_for
from sccp.version import PROTOCOL_VERSION
from sccp.wallet.service import WalletService

FAUCET = faucet_keypair("devnet")
FAUCET_ADDRESS = encode_address(FAUCET.public_key_bytes, DEVNET)
MINER = keypair_from_seed(bytes([9]) * 32)
MINER_ADDRESS = encode_address(MINER.public_key_bytes, DEVNET)


def _node(network: Any = DEVNET) -> NodeService:
    return NodeService.open(network)


def _client(service: NodeService) -> httpx.Client:
    return client_for(create_app(service))


def test_status_describes_the_chain() -> None:
    service = _node()
    with _client(service) as client:
        payload = client.get("/v1/status").json()
    assert payload["network"] == "devnet"
    assert payload["protocol_version"] == PROTOCOL_VERSION
    assert payload["height"] == 0
    assert payload["genesis_hash"] == service.genesis_hash
    assert payload["tip_hash"] == service.genesis_hash
    assert payload["peers"] == 0
    assert payload["mempool"] == {"transactions": 0, "proofs": 0}
    service.close()


def test_params_difficulty_and_challenges_routes() -> None:
    service = _node()
    with _client(service) as client:
        params = client.get("/v1/params").json()
        difficulty = client.get("/v1/difficulty").json()
        challenges = client.get("/v1/challenges").json()
    assert params["network_id"] == "devnet"
    assert difficulty["wins_required"] == DEVNET.wins_required
    assert difficulty["history"][0]["height"] == 0
    assert challenges == {"network": "devnet", "challenges": []}


def test_block_lookup_by_height_and_hash() -> None:
    service = _node()
    with _client(service) as client:
        genesis = client.get("/v1/blocks/0").json()
        by_hash = client.get(f"/v1/blocks/{service.genesis_hash}").json()
        missing = client.get("/v1/blocks/999")
    assert genesis["height"] == 0
    assert genesis["main_chain"] is True
    assert by_hash["hash"] == genesis["hash"]
    assert missing.status_code == 404


def test_account_view_for_the_faucet() -> None:
    service = _node()
    with _client(service) as client:
        payload = client.get(f"/v1/accounts/{FAUCET_ADDRESS}").json()
        invalid = client.get("/v1/accounts/sccpt1nope")
    assert payload["balance"] > 0
    assert payload["public_key"] == FAUCET.public_key_hex
    assert payload["registered_until_epoch"] is None
    assert invalid.status_code == 400


def test_transaction_lifecycle_through_the_api() -> None:
    service = _node()
    tx = WalletService(DEVNET, service.chain.state).build_transfer(FAUCET, MINER_ADDRESS, 5_000)
    with _client(service) as client:
        accepted = client.post("/v1/tx", json=tx.model_dump(mode="json", by_alias=True))
        pool = client.get("/v1/mempool").json()
        record = client.get(f"/v1/transactions/{txid(tx)}").json()
        unknown = client.get(f"/v1/transactions/{'00' * 32}")
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["txid"] == txid(tx)
    assert pool["transactions"] == [txid(tx)]
    assert record["in_mempool"] is True
    assert record["type"] == "transfer"
    assert unknown.status_code == 404


def test_invalid_and_foreign_transactions_are_refused() -> None:
    service = _node()
    signed = WalletService(DEVNET, service.chain.state).build_transfer(FAUCET, MINER_ADDRESS, 5_000)
    stale = signed.model_copy(update={"nonce": signed.nonce + 1})
    with _client(service) as client:
        rejected = client.post("/v1/tx", json=stale.model_dump(mode="json", by_alias=True))
    assert rejected.status_code == 400
    with pytest.raises(MempoolError, match="signature"):
        service.submit_transaction(stale)
    foreign = signed.model_copy(update={"network": TESTNET.network_id})
    with pytest.raises(ProtocolError, match="testnet"):
        service.submit_transaction(foreign)


def test_duplicate_pending_transactions_are_refused() -> None:
    service = _node()
    tx = WalletService(DEVNET, service.chain.state).build_transfer(FAUCET, MINER_ADDRESS, 5_000)
    service.submit_transaction(tx)
    with pytest.raises(MempoolError, match="already in the mempool"):
        service.submit_transaction(tx)


def test_ingesting_a_block_twice_is_idempotent(devnet_chain: Any, mine_block: Any) -> None:
    service = _node()
    block = mine_block(devnet_chain, MINER)
    first = service.ingest_block(block)
    second = service.ingest_block(block)
    assert first.accepted and not first.duplicate
    assert second.accepted and second.duplicate
    assert service.height == 1


def test_unknown_parents_wait_in_the_orphan_pool(devnet_chain: Any, mine_block: Any) -> None:
    service = _node()
    mine_block(devnet_chain, MINER)
    orphan = mine_block(devnet_chain, MINER)
    result = service.ingest_block(orphan)
    assert not result.accepted
    assert service.height == 0


def test_submit_block_route_accepts_and_202s_orphans(devnet_chain: Any, mine_block: Any) -> None:
    service = _node()
    parent = mine_block(devnet_chain, MINER)
    mine_block(devnet_chain, MINER)
    child = mine_block(devnet_chain, MINER)
    with _client(service) as client:
        accepted = client.post("/v1/blocks", json=parent.model_dump(mode="json"))
        orphaned = client.post("/v1/blocks", json=child.model_dump(mode="json"))
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["height"] == 1
    assert orphaned.status_code == 202
    assert service.height == 1


def test_epoch_game_and_match_routes_report_absence(devnet_chain: Any, mine_block: Any) -> None:
    service = _node()
    for _ in range(2):
        service.ingest_block(mine_block(devnet_chain, MINER))
    with _client(service) as client:
        epoch = client.get("/v1/epochs/1")
        early = client.get("/v1/epochs/0/beacon")
        unknown = client.get("/v1/epochs/99")
        game = client.get("/v1/games/unknown")
        match = client.get("/v1/matches/unknown")
        transcript = client.get("/v1/matches/unknown/transcript")
        peers = client.get("/v1/peers")
    assert epoch.status_code == 200
    assert epoch.json()["matchable"] is False
    assert early.status_code == 404
    assert unknown.status_code == 404
    assert game.status_code == 404
    assert match.status_code == 404
    assert transcript.status_code == 404
    assert peers.json() == []


def test_gossip_peer_envelopes_dedupe_and_apply() -> None:
    service = _node()
    for index in range(3):
        service.peers.add(f"http://peer-{index}")
    envelope = sign_envelope(
        MINER, network="devnet", kind=GossipKind.PEER, payload={"url": "http://new"}
    )
    with _client(service) as client:
        first = client.post("/v1/gossip", json=envelope.model_dump(mode="json"))
        second = client.post("/v1/gossip", json=envelope.model_dump(mode="json"))
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["duplicate"] is False
    assert body["object_id"] == "http://new"
    assert set(body["fanout"]) <= {*(f"http://peer-{index}" for index in range(3)), "http://new"}
    assert second.json()["duplicate"] is True
    assert "http://new" in service.peers
    excluded = service.handle_gossip(
        sign_envelope(MINER, network="devnet", kind=GossipKind.PEER, payload={"url": "http://z"}),
        peer="http://peer-1",
    )
    assert "http://peer-1" not in excluded.fanout


def test_gossip_relays_transactions_for_registered_senders() -> None:
    service = _node()
    tx = WalletService(DEVNET, service.chain.state).build_transfer(FAUCET, MINER_ADDRESS, 5_000)
    envelope = sign_envelope(
        FAUCET,
        network="devnet",
        kind=GossipKind.TRANSACTION,
        payload={"transaction": tx.model_dump(mode="json", by_alias=True)},
    )
    with _client(service) as client:
        result = client.post("/v1/gossip", json=envelope.model_dump(mode="json"))
    assert result.status_code == 200, result.text
    assert result.json()["object_id"] == txid(tx)
    assert len(service.mempool.transactions) == 1


def test_gossip_rejects_bad_signatures_and_foreign_networks() -> None:
    service = _node()
    envelope = sign_envelope(
        FAUCET, network="devnet", kind=GossipKind.PEER, payload={"url": "http://x"}
    )
    tampered = envelope.model_copy(update={"payload": {"url": "http://y"}})
    with _client(service) as client:
        bad_signature = client.post("/v1/gossip", json=tampered.model_dump(mode="json"))
    assert bad_signature.status_code == 400
    foreign = envelope.model_copy(update={"network": TESTNET.network_id})
    with pytest.raises(GossipError, match="testnet"):
        service.handle_gossip(foreign)


def test_gossip_verifies_envelopes_against_an_explicit_key() -> None:
    service = _node()
    envelope = sign_envelope(
        MINER, network="devnet", kind=GossipKind.PEER, payload={"url": "http://k"}
    )
    result = service.handle_gossip(envelope, public_key=MINER.public_key_hex)
    assert result.object_id == "http://k"


class _StubPeerClient:
    def __init__(self, *, status: dict[str, object], blocks: dict[str, object]) -> None:
        self._status = status
        self._blocks = blocks

    def status(self) -> dict[str, object]:
        return self._status

    def block(self, reference: str) -> dict[str, object]:
        return self._blocks[reference]  # type: ignore[return-value]


def test_sync_from_pulls_missing_blocks(devnet_chain: Any, mine_block: Any) -> None:
    source = _node()
    for _ in range(3):
        mine_block(source.chain, MINER)
    source_http = client_for(create_node_app(source))
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
    with client_for(create_node_app(other)) as http:
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
    with client_for(create_node_app(service)) as http:
        peer = HttpPeerClient("http://node", client=http)
        assert peer.status()["network"] == "devnet"
        with pytest.raises(GossipError, match="cannot fetch"):
            peer.block("999")


def test_compat_shim_serves_a_genesis_only_node() -> None:
    from sccp.node.routes import create_app as create_compat_app

    with client_for(create_compat_app(DEVNET)) as client:
        payload = client.get("/v1/status").json()
    assert payload["network"] == "devnet"
    assert payload["height"] == 0
