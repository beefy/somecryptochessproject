"""The deploy-time demo seed: one bridge leg and one SCCP/C960 pool."""

from __future__ import annotations

import json
from collections.abc import Iterator

import httpx
import pytest
from deploy import seed_testnet_demo as seed
from deploy.bootstrap_testnet_stack import BootstrapMiner

from sccp.chain import dex
from sccp.chain.genesis import testnet_bridge_preimage as bridge_preimage
from sccp.chain.state import HtlcStatus
from sccp.config import CHESS960_ASSET_ID, TESTNET, coins_to_units
from sccp.crypto.address import encode_address
from sccp.crypto.pq import hybrid_keypair_from_seed
from sccp.errors import ConfigError, RateLimitError
from sccp.node.service import NodeService
from sccp.protocol.models import Transaction


@pytest.fixture
def node() -> Iterator[NodeService]:
    """A fresh in-process testnet node at height 0, funded faucet included."""
    service = NodeService.open(TESTNET)
    try:
        yield service
    finally:
        service.close()


def _options(**overrides: object) -> seed.SeedOptions:
    """Fast polling for a unit test; the transactions settle through the mock."""
    values: dict[str, object] = {
        "wait_seconds": 1.0,
        "poll_seconds": 0.001,
    }
    values.update(overrides)
    return seed.SeedOptions(**values)  # type: ignore[arg-type]


def _account_payload(node: NodeService, address: str) -> dict[str, object]:
    """Serve accounts from the projection every pending transaction has touched."""
    account = node.mempool.projection.account(address)
    return {
        "network": TESTNET.network_id,
        "address": address,
        "balance": account.balance,
        "immature": account.immature,
        "bonded": account.bonded,
        "total": account.total,
        "nonce": account.nonce,
        "assets": dict(account.assets),
        "immature_assets": dict(account.immature_assets),
    }


def _transport(node: NodeService) -> httpx.MockTransport:
    """Route the seeder's requests into the real node.

    The pool and HTLC views come from the node's own projection, and every
    ``POST /v1/tx`` goes through the production admission path. Reporting an
    empty mempool and a mined height stands in for the block the node settles
    between the seeder's steps.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/status":
            return httpx.Response(200, json={"network": TESTNET.network_id, "height": 19})
        if path == "/v1/mempool":
            return httpx.Response(200, json={"transactions": [], "proofs": 0})
        if path == "/v1/pools":
            pools = [
                {
                    "pool_id": pool.pool_id,
                    "network": TESTNET.network_id,
                    "asset_a": pool.asset_a or None,
                    "asset_b": pool.asset_b or None,
                    "reserve_a": pool.reserve_a,
                    "reserve_b": pool.reserve_b,
                    "total_shares": pool.total_shares,
                    "fee_bps": TESTNET.dex_fee_bps,
                    "shares": dict(sorted(pool.shares.items())),
                }
                for pool in node.mempool.projection.pools.values()
            ]
            return httpx.Response(200, json={"network": TESTNET.network_id, "pools": pools})
        if path == "/v1/htlcs":
            address = request.url.params.get("address")
            records = [
                record
                for record in node.mempool.projection.htlcs.values()
                if address is None or address in (record.sender, record.beneficiary)
            ]
            htlcs = [node._htlc_view(record).model_dump(mode="json") for record in records]
            return httpx.Response(200, json={"network": TESTNET.network_id, "htlcs": htlcs})
        if path.startswith("/v1/accounts/"):
            address = path.removeprefix("/v1/accounts/")
            return httpx.Response(200, json=_account_payload(node, address))
        if path == "/v1/faucet":
            body = json.loads(request.content)
            try:
                view = node.faucet_request(
                    body["address"],
                    client="seed",
                    amount=body.get("amount"),
                )
            except RateLimitError as exc:
                return httpx.Response(429, json={"error": str(exc)})
            return httpx.Response(200, json=view.model_dump(mode="json"))
        if path == "/v1/tx":
            tx = Transaction.model_validate(json.loads(request.content))
            accepted = node.submit_transaction(tx)  # a refusal fails the test here
            return httpx.Response(200, json=accepted.model_dump(mode="json"))
        if path.startswith("/v1/transactions/"):
            digest = path.removeprefix("/v1/transactions/")
            if digest not in node.mempool.transactions:
                return httpx.Response(404, json={"error": "unknown transaction"})
            return httpx.Response(
                200,
                json={
                    "txid": digest,
                    "network": TESTNET.network_id,
                    "height": 20,
                },
            )
        raise AssertionError(f"unexpected request {request.url}")

    return httpx.MockTransport(handler)


def _miner(index: int = 2) -> BootstrapMiner:
    """One Chess960 stack miner, derived from the published init seed."""
    keypair = hybrid_keypair_from_seed(bytes([0xA0 + index]) * 32)
    return BootstrapMiner(
        name=f"miner-{index + 1}",
        variant="chess960",
        address=encode_address(keypair.public_key_bytes, TESTNET),
        keypair=keypair,
        nonce_secret=f"{index + 1:02x}" * 32,
    )


def _seed_account(
    node: NodeService,
    *,
    address: str,
    native_coins: int,
    c960_coins: int = 0,
    public_key: str | None = None,
) -> None:
    """Credit a miner in both the tip and the projection, as a mined block would."""
    for state in (node.chain.state, node.mempool.projection):
        account = state.ensure(address)
        account.balance = coins_to_units(native_coins)
        if c960_coins:
            account.assets[CHESS960_ASSET_ID] = coins_to_units(c960_coins)
        if public_key is not None:
            account.public_key = public_key


def test_demo_wallets_are_deterministic_and_distinct() -> None:
    """A redeploy seats the same demo accounts without persisting anything."""
    first = seed.demo_wallet(0)
    assert first.address == seed.demo_wallet(0).address
    assert first.address != seed.demo_wallet(1).address
    assert first.address.startswith("sccpt1")
    with pytest.raises(ConfigError):
        seed.demo_wallet(-1)


def test_a_fresh_deploy_already_has_the_genesis_bridge_and_pool(
    node: NodeService,
) -> None:
    """The testnet genesis itself carries the demo pool and completed bridge."""
    miner = _miner()
    _seed_account(
        node,
        address=miner.address,
        native_coins=100,
        c960_coins=50,
        public_key=miner.keypair.public_key_hex,
    )
    options = _options()

    with httpx.Client(transport=_transport(node), base_url="http://node") as client:
        report = seed.seed_once(client, miners=(miner,), options=options)

    assert report.bridge == "bridge: demo leg already on chain"
    assert report.dex.startswith("dex: SCCP/C960 pool ")
    assert report.dex.endswith("already exists")

    liquidity = seed.demo_wallet(0)
    pool_id = dex.pool_id(dex.NATIVE_ASSET, CHESS960_ASSET_ID)
    pool = node.mempool.projection.pools[pool_id]
    assert (pool.asset_a, pool.asset_b) == (dex.NATIVE_ASSET, CHESS960_ASSET_ID)
    assert pool.reserve_a == pool.reserve_b == coins_to_units(seed.DEX_COINS)
    assert pool.total_shares == dex.initial_shares(pool.reserve_a, pool.reserve_b)
    assert pool.shares == {liquidity.address: pool.total_shares}

    miner_account = node.mempool.projection.account(miner.address)
    assert miner_account.assets[CHESS960_ASSET_ID] == coins_to_units(50)

    htlcs = list(node.mempool.projection.htlcs.values())
    assert len(htlcs) == 1
    assert htlcs[0].status is HtlcStatus.CLAIMED
    assert htlcs[0].sender == liquidity.address
    assert htlcs[0].beneficiary == seed.demo_wallet(1).address
    assert htlcs[0].preimage == bridge_preimage()


def test_the_bridge_leg_is_not_duplicated_when_the_seeder_restarts(
    node: NodeService,
) -> None:
    """The HTLC check makes a restart after a partial run safe."""
    miner = _miner()
    _seed_account(
        node,
        address=miner.address,
        native_coins=100,
        c960_coins=50,
        public_key=miner.keypair.public_key_hex,
    )
    options = _options()
    with httpx.Client(transport=_transport(node), base_url="http://node") as client:
        seed.seed_once(client, miners=(miner,), options=options)
        second = seed.seed_once(client, miners=(miner,), options=options)

    assert second.bridge == "bridge: demo leg already on chain"
    assert second.dex.startswith("dex: SCCP/C960 pool ")
    assert "already exists" in second.dex
    assert len(node.mempool.projection.htlcs) == 1


def test_the_dex_seed_times_out_when_no_pool_or_c960_exists(node: NodeService) -> None:
    """Without the genesis pool, a chain with no Chess960 games reports the wait."""
    miner = _miner()
    _seed_account(
        node,
        address=miner.address,
        native_coins=100,
        public_key=miner.keypair.public_key_hex,
    )
    node.chain.state.pools.clear()
    node.mempool.projection.pools.clear()
    with (
        httpx.Client(transport=_transport(node), base_url="http://node") as client,
        pytest.raises(seed.SeedError, match="no mature C960 arrived"),
    ):
        seed.seed_dex(
            client,
            miners=(miner,),
            liquidity=seed.demo_wallet(0),
            options=_options(wait_seconds=0.01),
        )

    assert not node.mempool.projection.pools
