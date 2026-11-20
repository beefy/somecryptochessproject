"""The node's HTTP API, exercised in-process over ASGI.

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
from fastapi.routing import APIRoute

from sccp.chain.dex import NATIVE_ASSET
from sccp.chain.dex import pool_id as derive_pool_id
from sccp.chain.genesis import faucet_keypair
from sccp.chain.mempool import Mempool
from sccp.chain.state import Pool, ProofRecord
from sccp.chain.tx import txid
from sccp.config import CHESS960_ASSET_ID, DEVNET, TESTNET
from sccp.crypto.address import encode_address
from sccp.crypto.pq import hybrid_keypair_from_seed
from sccp.errors import MempoolError, ProtocolError
from sccp.node.app import OPENAPI_TAGS, create_app, openapi_contract
from sccp.node.service import NodeService
from sccp.transport import client_for
from sccp.version import PROTOCOL_VERSION
from sccp.wallet.service import WalletService

FAUCET = faucet_keypair("devnet")
FAUCET_ADDRESS = encode_address(FAUCET.public_key_bytes, DEVNET)
MINER = hybrid_keypair_from_seed(bytes([9]) * 32)
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
    assert payload["minted"] >= 0
    assert payload["burned"] == 0
    assert isinstance(payload["minted_assets"], dict)
    # The relay's pairing gate reads this: a fresh devnet has no witnesses yet.
    assert payload["witnesses"] == 0
    service.close()


def test_status_reports_the_native_burn_counter() -> None:
    service = _node()
    service.chain.state.burned = 123
    with _client(service) as client:
        payload = client.get("/v1/status").json()
    assert payload["burned"] == 123
    service.close()


def test_params_and_difficulty_routes() -> None:
    service = _node()
    with _client(service) as client:
        params = client.get("/v1/params").json()
        difficulty = client.get("/v1/difficulty").json()
        challenges = client.get("/v1/challenges")
    assert params["network_id"] == "devnet"
    assert difficulty["wins_required"] == DEVNET.wins_required
    assert difficulty["history"][0]["height"] == 0
    assert challenges.status_code == 404


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
    assert payload["variant"] == "standard"
    assert payload["assets"] == {}
    assert payload["immature_assets"] == {}
    assert invalid.status_code == 400


def test_pool_routes_report_reserves_and_positions() -> None:
    service = _node()
    pool_id = derive_pool_id(NATIVE_ASSET, CHESS960_ASSET_ID)
    service.chain.state.pools[pool_id] = Pool(
        pool_id=pool_id,
        asset_a=NATIVE_ASSET,
        asset_b=CHESS960_ASSET_ID,
        reserve_a=10_000,
        reserve_b=20_000,
        total_shares=1_000,
        shares={FAUCET_ADDRESS: 1_000},
    )
    with _client(service) as client:
        pools = client.get("/v1/pools").json()
        one = client.get(f"/v1/pools/{pool_id}").json()
        missing = client.get(f"/v1/pools/{'ab' * 32}")
    assert pools["network"] == "devnet"
    assert len(pools["pools"]) == 1
    assert pools["pools"][0]["pool_id"] == pool_id
    assert one["asset_a"] is None
    assert one["asset_b"] == CHESS960_ASSET_ID
    assert one["reserve_a"] == 10_000
    assert one["reserve_b"] == 20_000
    assert one["total_shares"] == 1_000
    assert one["fee_bps"] == DEVNET.dex_fee_bps
    assert one["shares"] == {FAUCET_ADDRESS: 1_000}
    assert missing.status_code == 404
    service.close()


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


def test_a_payload_canonical_json_cannot_encode_is_a_bad_request() -> None:
    """Admission hashes the payload before validating it, so both cases are a 400."""
    service = _node()
    tx = WalletService(DEVNET, service.chain.state).build_transfer(FAUCET, MINER_ADDRESS, 5_000)
    as_float = tx.model_copy(update={"payload": {**tx.payload, "memo": 1.5}})
    as_surrogate = tx.model_copy(update={"payload": {**tx.payload, "memo": "\ud800"}})
    # json.dumps escapes the lone surrogate, so the request body is valid UTF-8
    # even though the decoded payload is not, and the terminal would not show it.
    escaped = json.dumps(as_surrogate.model_dump(mode="json", by_alias=True))
    with _client(service) as client:
        float_response = client.post("/v1/tx", json=as_float.model_dump(mode="json", by_alias=True))
        surrogate_response = client.post(
            "/v1/tx", content=escaped, headers={"content-type": "application/json"}
        )
        pool = client.get("/v1/mempool").json()
    assert float_response.status_code == 400, float_response.text
    assert surrogate_response.status_code == 400, surrogate_response.text
    assert "surrogate" in surrogate_response.json()["detail"]
    assert pool["transactions"] == []


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


# --- the explorer's list endpoints -----------------------------------------


def test_block_list_walks_back_from_the_tip(devnet_chain: Any, mine_block: Any) -> None:
    """``GET /v1/blocks`` is newest first and pages with ``before_height``."""
    service = _node()
    for _ in range(3):
        service.ingest_block(mine_block(devnet_chain, MINER))
    with _client(service) as client:
        page = client.get("/v1/blocks?limit=2").json()
        older = client.get("/v1/blocks?limit=2&before_height=2").json()
        too_many = client.get("/v1/blocks?limit=500")
    assert page["network"] == "devnet"
    assert page["tip_height"] == 3
    assert [row["height"] for row in page["blocks"]] == [3, 2]
    assert [row["height"] for row in older["blocks"]] == [1, 0]
    assert page["blocks"][0]["miner"] == MINER_ADDRESS
    assert page["blocks"][0]["parent_hash"] == page["blocks"][1]["hash"]
    assert isinstance(page["blocks"][0]["variants"], dict)
    assert too_many.status_code == 422
    service.close()


def test_game_list_shows_pending_then_settled_results(
    live_chain: Any, chain_match: Any, mine_block: Any, miner_keys: dict[str, Any]
) -> None:
    """A proof in the pool lists as pending; once mined the row carries the result."""
    chain = live_chain.chain
    match = chain_match(chain, epoch=3)
    service = NodeService(
        params=chain.params,
        chain=chain,
        mempool=Mempool(params=chain.params, tip_state=chain.state, book=chain.book),
    )
    service.mempool.add_proof(match.proof)
    with _client(service) as client:
        pending = client.get("/v1/games").json()["games"]
        pending_view = client.get(f"/v1/games/{match.proof.game_hash}").json()
    assert [row["game_hash"] for row in pending] == [match.proof.game_hash]
    assert pending_view["variant"] == match.proof.variant
    assert pending[0]["in_mempool"] is True
    assert pending[0]["height"] is None
    assert pending[0]["result"] == match.proof.result
    assert pending[0]["variant"] == match.proof.variant

    mine_block(chain, miner_keys[match.proof.winner], proofs=(match.proof,))
    service.mempool = Mempool(params=chain.params, tip_state=chain.state, book=chain.book)
    with _client(service) as client:
        settled = client.get("/v1/games").json()["games"]
        older = client.get(f"/v1/games?before_height={chain.height}").json()["games"]
    assert [row["game_hash"] for row in settled] == [match.proof.game_hash]
    row = settled[0]
    assert row["height"] == chain.height
    assert row["in_mempool"] is False
    assert row["winner"] == match.proof.winner
    assert row["loser"] == match.proof.loser
    assert row["winner_seat"] == match.proof.winner_seat
    assert row["termination"] == match.proof.termination
    assert row["plies"] == match.proof.plies
    assert row["epoch"] == match.proof.epoch
    assert row["match_id"] == match.proof.match_id
    assert row["variant"] == match.proof.variant
    assert older == []
    service.close()


def test_game_list_cursor_breaks_a_height_tie() -> None:
    """The game cursor breaks a height tie, so a block's proofs page exactly.

    A block can settle several proofs. Paging by height alone would drop every
    game after the first one in that block; ``before_game_hash`` is the
    tie-break that makes the next page start where the last one stopped. The
    records are seeded directly: the point here is the list cursor, not mining.
    """
    service = _node()
    for suffix in ("11", "22", "33"):
        game_hash = suffix * 32
        service.chain.state.proofs[game_hash] = ProofRecord(
            proof_id=f"proof-{suffix}",
            game_hash=game_hash,
            height=1,
            winner=MINER_ADDRESS,
        )
    with _client(service) as client:
        everything = client.get("/v1/games?limit=200").json()["games"]
        first = client.get("/v1/games?limit=1").json()["games"]
        cursor = first[0]
        rest = client.get(
            f"/v1/games?limit=200&before_height={cursor['height']}"
            f"&before_game_hash={cursor['game_hash']}"
        ).json()["games"]
        height_only = client.get(f"/v1/games?before_height={cursor['height']}").json()["games"]
    assert [row["game_hash"] for row in everything] == ["11" * 32, "22" * 32, "33" * 32]
    assert [row["game_hash"] for row in first] == [everything[0]["game_hash"]]
    assert [row["game_hash"] for row in rest] == [row["game_hash"] for row in everything[1:]]
    assert height_only == []
    service.close()


def test_game_list_rejects_an_empty_cursor_hash() -> None:
    """An empty hash is a caller bug, not a cursor that happens to match."""
    with _client(_node()) as client:
        response = client.get("/v1/games?before_height=1&before_game_hash=")
    assert response.status_code == 422


def test_game_pgn_route_serves_the_proof_body(
    live_chain: Any, chain_match: Any, mine_block: Any, miner_keys: dict[str, Any]
) -> None:
    """``GET /v1/games/{hash}/pgn`` quotes the proof, transcript or not.

    The moves live in the proof body, so the game page can show the PGN for a
    match this node never cached; once mined the same body still answers.
    """
    chain = live_chain.chain
    match = chain_match(chain, epoch=3)
    service = NodeService(
        params=chain.params,
        chain=chain,
        mempool=Mempool(params=chain.params, tip_state=chain.state, book=chain.book),
    )
    service.mempool.add_proof(match.proof)
    with _client(service) as client:
        pending = client.get(f"/v1/games/{match.proof.game_hash}/pgn")
        unknown = client.get("/v1/games/unknown/pgn")
    assert pending.status_code == 200, pending.text
    assert pending.json() == {
        "game_hash": match.proof.game_hash,
        "network": "devnet",
        "pgn": match.proof.pgn,
    }
    assert unknown.status_code == 404

    mine_block(chain, miner_keys[match.proof.winner], proofs=(match.proof,))
    service.mempool = Mempool(params=chain.params, tip_state=chain.state, book=chain.book)
    with _client(service) as client:
        settled = client.get(f"/v1/games/{match.proof.game_hash}/pgn").json()
    assert settled["pgn"] == match.proof.pgn
    service.close()


def test_ratings_routes_are_derived_from_settled_proofs(
    live_chain: Any, chain_match: Any, mine_block: Any, miner_keys: dict[str, Any]
) -> None:
    """A rating is replayed from settled proofs, never submitted by a miner."""
    chain = live_chain.chain
    match = chain_match(chain, epoch=3)
    mine_block(chain, miner_keys[match.proof.winner], proofs=(match.proof,))
    service = NodeService(
        params=chain.params,
        chain=chain,
        mempool=Mempool(params=chain.params, tip_state=chain.state, book=chain.book),
    )
    unrated = encode_address(hybrid_keypair_from_seed(bytes([9]) * 32).public_key_bytes, DEVNET)
    with _client(service) as client:
        board = client.get("/v1/ratings").json()
        capped = client.get("/v1/ratings?limit=1").json()
        detail = client.get(f"/v1/ratings/{match.proof.winner}").json()
        prior = client.get(f"/v1/ratings/{unrated}")
        invalid = client.get("/v1/ratings/not-an-address")
    assert board["network"] == "devnet"
    assert board["algorithm"] == "glicko-2"
    assert board["rating_period"] == "epoch"
    assert board["games"] == 1
    assert [row["rank"] for row in board["miners"]] == [1, 2]
    assert board["miners"][0]["address"] == match.proof.winner
    assert board["distribution"]["rated"] == 2
    assert sum(bucket["count"] for bucket in board["distribution"]["buckets"]) == 2
    assert capped["miners"] == board["miners"][:1]
    assert detail["rank"] == 1
    assert detail["games"] == 1
    assert detail["wins"] == 1 and detail["losses"] == 0
    assert detail["history"][-1]["games"] == 1
    assert detail["history"][-1]["rating"] == detail["rating"]
    assert detail["variant"] == "standard"
    assert all(row["variant"] == "standard" for row in board["miners"])
    assert prior.status_code == 200
    assert prior.json()["rating"] == 1500.0
    assert prior.json()["rank"] is None
    assert prior.json()["history"] == []
    assert invalid.status_code == 400
    service.close()


def test_ratings_on_a_genesis_only_node_are_empty() -> None:
    service = _node()
    with _client(service) as client:
        board = client.get("/v1/ratings").json()
    assert board["miners"] == []
    assert board["rated_epochs"] == 0
    assert board["distribution"]["rated"] == 0
    assert board["distribution"]["mean"] == 0.0
    assert sum(bucket["count"] for bucket in board["distribution"]["buckets"]) == 0
    service.close()


def test_a_built_explorer_is_served_next_to_the_api(tmp_path: Path) -> None:
    """``web_root`` mounts a static client without shadowing the JSON API."""
    (tmp_path / "index.html").write_text("<!doctype html><title>explorer</title>", encoding="utf-8")
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    service = _node()
    with client_for(create_app(service, web_root=tmp_path)) as client:
        page = client.get("/")
        asset = client.get("/assets/app.js")
        api = client.get("/v1/status")
    assert page.status_code == 200
    assert "explorer" in page.text
    assert asset.status_code == 200
    assert api.json()["network"] == "devnet"
    service.close()


def test_web_dist_root_honours_the_environment_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A container points ``SCCP_WEB_DIST`` at whatever it built."""
    from sccp.node.app import WEB_DIST_ENV, web_dist_root

    monkeypatch.setenv(WEB_DIST_ENV, str(tmp_path))
    assert web_dist_root() is None
    (tmp_path / "index.html").write_text("<!doctype html>", encoding="utf-8")
    assert web_dist_root() == tmp_path


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
    """Every top-level JSON response names its network (PROTOCOL.md section 14)."""
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


# --- the generated documentation -------------------------------------------


def test_the_swagger_ui_the_spec_and_redoc_are_served() -> None:
    """FastAPI generates all three; nothing else has to be deployed for them."""
    service = _node()
    app = create_app(service)
    with client_for(app) as client:
        spec = client.get("/openapi.json")
        docs = client.get("/docs")
        redoc = client.get("/redoc")
    service.close()
    assert spec.status_code == 200
    assert spec.json()["info"]["title"] == "sccp node (devnet)"
    assert "PROTOCOL.md" in spec.json()["info"]["description"]
    assert docs.status_code == 200
    assert docs.headers["content-type"].startswith("text/html")
    assert "/openapi.json" in docs.text
    assert redoc.status_code == 200
    assert "/openapi.json" in redoc.text


def test_the_explorer_mount_does_not_shadow_the_documentation(tmp_path: Path) -> None:
    """The explorer is mounted at ``/``, so ``/docs`` could have been swallowed."""
    (tmp_path / "index.html").write_text("<!doctype html><title>explorer</title>", encoding="utf-8")
    service = _node()
    with client_for(create_app(service, web_root=tmp_path)) as client:
        docs = client.get("/docs")
        spec = client.get("/openapi.json")
    service.close()
    assert docs.status_code == 200
    assert "swagger-ui" in docs.text
    assert spec.status_code == 200


def test_every_route_is_documented_and_grouped() -> None:
    """An untagged route would fall outside every Swagger section."""
    service = _node()
    app = create_app(service)
    schema = app.openapi()
    registered = {
        route.path
        for route in app.routes
        if isinstance(route, APIRoute) and route.path.startswith("/v1/")
    }
    service.close()
    documented = {path for path in schema["paths"] if path.startswith("/v1/")}
    assert documented == registered
    assert [tag["name"] for tag in schema["tags"]] == [tag["name"] for tag in OPENAPI_TAGS]
    sections = {tag["name"] for tag in OPENAPI_TAGS}
    for path, operations in schema["paths"].items():
        for operation in operations.values():
            assert operation.get("tags"), path
            assert set(operation["tags"]) <= sections, path


def test_the_pinned_contract_carries_no_documentation_prose() -> None:
    """Grouping and prose may change freely; the wire format may not."""
    schema = _contract()
    assert "tags" not in schema
    for path, operations in schema["paths"].items():
        for operation in operations.values():
            assert "tags" not in operation, path
            assert "summary" not in operation, path
            assert "description" not in operation, path
