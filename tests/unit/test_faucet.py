"""The testnet faucet endpoint."""

from __future__ import annotations

import httpx
import pytest

from sccp.chain.tx import txid
from sccp.config import DEVNET
from sccp.crypto.address import encode_address
from sccp.crypto.keys import keypair_from_seed
from sccp.errors import FaucetError, RateLimitError
from sccp.node.app import create_app
from sccp.node.faucet import FaucetPolicy
from sccp.node.service import NodeService
from sccp.transport import client_for

RECIPIENT = encode_address(keypair_from_seed(bytes([7]) * 32).public_key_bytes, DEVNET)


def _client(service: NodeService) -> httpx.Client:
    return client_for(create_app(service))


def _limited(service: NodeService) -> None:
    assert service.faucet is not None
    service.faucet.policy = FaucetPolicy(
        amount_coins=5,
        max_coins=10,
        cooldown_seconds=60,
        client_window_seconds=60,
        client_grants=1,
    )


def test_faucet_status_and_grant() -> None:
    service = NodeService.open(DEVNET)
    _limited(service)
    with _client(service) as client:
        status = client.get("/v1/faucet")
        granted = client.post("/v1/faucet", json={"address": RECIPIENT})
    assert status.status_code == 200
    assert status.json()["amount"] == 5
    assert status.json()["address"] == service.faucet.address
    assert granted.status_code == 200, granted.text
    payload = granted.json()
    assert payload["address"] == RECIPIENT
    assert payload["amount"] == 5
    assert payload["txid"] in service.mempool.transactions
    service.close()


def test_faucet_rate_limits_the_address_and_the_client() -> None:
    service = NodeService.open(DEVNET)
    _limited(service)
    assert service.faucet is not None
    service.faucet_request(RECIPIENT, client="first")
    with pytest.raises(RateLimitError) as address_limit:
        service.faucet_request(RECIPIENT, client="second")
    assert address_limit.value.retry_after > 0
    other = encode_address(keypair_from_seed(bytes([8]) * 32).public_key_bytes, DEVNET)
    with pytest.raises(RateLimitError, match="client first"):
        service.faucet_request(other, client="first")
    service.close()


def test_faucet_refuses_bad_amounts_and_addresses() -> None:
    service = NodeService.open(DEVNET)
    _limited(service)
    with pytest.raises(FaucetError, match="between"):
        service.faucet_request(RECIPIENT, client="client", amount=11)
    with pytest.raises(FaucetError, match="valid"):
        service.faucet_request("sccpt1nope", client="client")
    assert not service.mempool.transactions
    service.close()


def test_the_api_reports_rate_limits_with_retry_after() -> None:
    service = NodeService.open(DEVNET)
    _limited(service)
    with _client(service) as client:
        assert client.post("/v1/faucet", json={"address": RECIPIENT}).status_code == 200
        limited = client.post("/v1/faucet", json={"address": RECIPIENT})
    assert limited.status_code == 429
    assert int(limited.headers["Retry-After"]) > 0
    service.close()


def test_a_faucet_grant_is_signed_by_the_faucet_key() -> None:
    service = NodeService.open(DEVNET)
    view = service.faucet_request(RECIPIENT, client="client")
    tx = service.mempool.transactions[view.txid].tx
    assert txid(tx) == view.txid
    assert tx.sender == service.faucet.address
    assert tx.payload["to"] == RECIPIENT
    service.close()
