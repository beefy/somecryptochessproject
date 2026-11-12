"""The node's HTTP API, exercised in-process over ASGI (M4).

Pull-sync lives in ``test_sync.py`` and gossip in ``test_gossip.py``; this file
covers the routes themselves, plus the OpenAPI snapshot that makes an
accidental breaking change to the contract visible in review.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from sccp.chain.genesis import faucet_keypair
from sccp.chain.tx import txid
from sccp.config import DEVNET, TESTNET
from sccp.crypto.address import encode_address
from sccp.crypto.keys import keypair_from_seed
from sccp.errors import MempoolError, ProtocolError
from sccp.node.app import create_app, openapi_contract
from sccp.node.service import NodeService
from sccp.transport import client_for
from sccp.version import PROTOCOL_VERSION
from sccp.wallet.service import WalletService

FAUCET = faucet_keypair("devnet")
FAUCET_ADDRESS = encode_address(FAUCET.public_key_bytes, DEVNET)
MINER = keypair_from_seed(bytes([9]) * 32)
MINER_ADDRESS = encode_address(MINER.public_key_bytes, DEVNET)
OPENAPI_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "openapi.json"


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


def test_compat_shim_serves_a_genesis_only_node() -> None:
    from sccp.node.routes import create_app as create_compat_app

    with client_for(create_compat_app(DEVNET)) as client:
        payload = client.get("/v1/status").json()
    assert payload["network"] == "devnet"
    assert payload["height"] == 0


# --- the OpenAPI contract ---------------------------------------------------


def _contract() -> dict[str, Any]:
    """The current schema, without prose or the package-version-dependent info."""
    return openapi_contract(create_app(NodeService.open(DEVNET)))


def test_openapi_contract_has_not_changed() -> None:
    """A route, field or status code change must update the fixture on purpose."""
    expected = json.loads(OPENAPI_FIXTURE.read_text(encoding="utf-8"))
    assert _contract() == expected, (
        "the node API changed; regenerate tests/fixtures/openapi.json "
        "with scripts/make_openapi_fixture.py if the change is intended"
    )


def test_openapi_contract_requires_the_network_id() -> None:
    """Every top-level JSON response names its network (plan section 5.12)."""
    schema = _contract()
    components = schema["components"]["schemas"]
    checked = 0
    for path, operations in schema["paths"].items():
        assert path.startswith("/v1/"), path
        for operation in operations.values():
            for code, response in operation["responses"].items():
                if not code.startswith("2"):
                    continue  # 422/404/400 carry the shared error envelope
                body = response.get("content", {}).get("application/json", {}).get("schema")
                if not body or "$ref" not in body:
                    continue
                model = components[body["$ref"].rsplit("/", 1)[-1]]
                while model.get("type") == "array":
                    model = components[model["items"]["$ref"].rsplit("/", 1)[-1]]
                if "certificate" in model.get("properties", {}):
                    # A transcript's certificate names the network; the wire
                    # format is signed as a certificate, not as a transcript.
                    assert "network" in components["MatchCertificate"]["properties"]
                    checked += 1
                    continue
                assert "network" in model.get("properties", {}), path
                checked += 1
    assert checked >= 12
