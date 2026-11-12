"""The testnet traffic drip that keeps an epoch supplied with ledger work."""

from __future__ import annotations

import json
from collections.abc import Iterator

import httpx
import pytest
from deploy import drip_testnet_traffic as drip

from sccp.chain.genesis import faucet_keypair
from sccp.config import TESTNET, NetworkParams, coins_to_units
from sccp.crypto.address import encode_address
from sccp.node.service import NodeService
from sccp.protocol.models import Transaction, TxType


@pytest.fixture
def node() -> Iterator[NodeService]:
    """A fresh in-process testnet node at height 0, funded faucet included."""
    service = NodeService.open(TESTNET)
    try:
        yield service
    finally:
        service.close()


def test_a_drip_transfer_is_an_ordinary_node_admitted_transaction(node: NodeService) -> None:
    """The drip signs what any wallet signs, so the node accepts it unchanged."""
    keypair = faucet_keypair(TESTNET.network_id)
    sender = encode_address(keypair.public_key_bytes, TESTNET)
    tx = drip.drip_transaction(TESTNET, nonce=node.chain.state.account(sender).nonce)
    assert tx.sender == sender
    assert tx.type is TxType.TRANSFER
    assert tx.payload == {
        "to": drip.sink_address(),
        "amount": coins_to_units(1),
        "public_key": keypair.public_key_hex,
    }
    assert node.submit_transaction(tx).accepted
    assert node.status().mempool.transactions == 1


def test_the_drip_pays_a_recipient_of_its_own() -> None:
    """Faucet-to-faucet is illegal, so the default sink is a separate address."""
    assert drip.sink_address() != encode_address(
        faucet_keypair(TESTNET.network_id).public_key_bytes, TESTNET
    )
    assert drip.sink_address().startswith("sccpt1")


def test_pool_needs_work_reads_the_status_mempool() -> None:
    assert drip.pool_needs_work({"mempool": {"transactions": 0, "proofs": 3}})
    assert not drip.pool_needs_work({"mempool": {"transactions": 1, "proofs": 0}})
    with pytest.raises(drip.DripError):
        drip.pool_needs_work({"height": 20})
    with pytest.raises(drip.DripError):
        drip.pool_needs_work({"mempool": {"transactions": "one"}})


def _status(pending: int) -> dict[str, object]:
    return {"height": 20, "mempool": {"transactions": pending, "proofs": 0}}


def _account(params: NetworkParams = TESTNET) -> dict[str, object]:
    sender = encode_address(faucet_keypair(params.network_id).public_key_bytes, params)
    return {
        "network": params.network_id,
        "address": sender,
        "balance": coins_to_units(1_000),
        "immature": 0,
        "bonded": 0,
        "total": coins_to_units(1_000),
        "nonce": 0,
    }


def test_the_drip_submits_once_into_an_empty_pool(node: NodeService) -> None:
    """A pool with work is left alone; an empty one gets exactly one transfer."""
    pending = {"count": 0}
    submitted: list[Transaction] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/status":
            return httpx.Response(200, json=_status(pending["count"]))
        if path.startswith("/v1/accounts/"):
            return httpx.Response(200, json=_account())
        if path == "/v1/tx":
            tx = Transaction.model_validate(json.loads(request.content))
            assert node.submit_transaction(tx).accepted  # the node validates it
            submitted.append(tx)
            pending["count"] += 1
            return httpx.Response(
                200, json={"network": "testnet", "txid": "ab" * 32, "accepted": True}
            )
        raise AssertionError(f"unexpected request {request.url}")

    with httpx.Client(transport=httpx.MockTransport(handler), base_url="http://node") as client:
        assert drip.run(client, rounds=3, poll_seconds=0) == 1
    assert [tx.nonce for tx in submitted] == [0]
    assert submitted[0].payload["to"] == drip.sink_address()


def test_the_drip_reports_a_refused_transfer(capsys: pytest.CaptureFixture[str]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/status":
            return httpx.Response(200, json=_status(0))
        if request.url.path.startswith("/v1/accounts/"):
            return httpx.Response(200, json=_account())
        return httpx.Response(400, json={"error": "fee 0 is below the 1-unit minimum"})

    with httpx.Client(transport=httpx.MockTransport(handler), base_url="http://node") as client:
        assert drip.run(client, rounds=1, poll_seconds=0) == 0
    assert "the node refused the transfer" in capsys.readouterr().err


def test_the_drip_survives_a_node_that_is_not_ready_yet(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A restarting node answers 503; the drip must retry, not exit."""
    attempts = {"status": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/status":
            attempts["status"] += 1
            if attempts["status"] == 1:
                return httpx.Response(503, text="starting up")
            return httpx.Response(200, json=_status(0))
        if path.startswith("/v1/accounts/"):
            return httpx.Response(200, json=_account())
        return httpx.Response(200, json={"network": "testnet", "txid": "cd" * 32})

    with httpx.Client(transport=httpx.MockTransport(handler), base_url="http://node") as client:
        assert drip.run(client, rounds=2, poll_seconds=0) == 1
    captured = capsys.readouterr()
    assert "HTTP 503" in captured.err
    assert "dripped" in captured.out


def test_the_drip_reports_a_faucet_that_cannot_afford_a_transfer(
    capsys: pytest.CaptureFixture[str],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/status":
            return httpx.Response(200, json=_status(0))
        if request.url.path.startswith("/v1/accounts/"):
            return httpx.Response(200, json={**_account(), "balance": 1})
        raise AssertionError(f"unexpected request {request.url}")

    with httpx.Client(transport=httpx.MockTransport(handler), base_url="http://node") as client:
        assert drip.run(client, rounds=1, poll_seconds=0) == 0
    assert "the faucet holds 1 units" in capsys.readouterr().err
