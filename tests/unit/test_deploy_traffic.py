"""The synthetic traffic generator that keeps the hosted testnet in use."""

from __future__ import annotations

import json
import random
from collections.abc import Iterator

import httpx
import pytest
from deploy import synthetic_testnet_traffic as traffic

from sccp.chain.genesis import faucet_keypair
from sccp.config import TESTNET, coins_to_units
from sccp.crypto.address import encode_address, validate_address
from sccp.node.app import create_app as create_node_app
from sccp.node.service import NodeService
from sccp.protocol.models import Transaction, TxType
from sccp.transport import client_for


@pytest.fixture
def node() -> Iterator[NodeService]:
    """A fresh in-process testnet node at height 0, funded faucet included."""
    service = NodeService.open(TESTNET)
    try:
        yield service
    finally:
        service.close()


def _faucet_address() -> str:
    return encode_address(faucet_keypair(TESTNET.network_id).public_key_bytes, TESTNET)


def _account_payload(node: NodeService, address: str) -> dict[str, object]:
    """Serve accounts from the projection the node's own faucet spends against."""
    account = node.mempool.projection.account(address)
    return {
        "network": TESTNET.network_id,
        "address": address,
        "balance": account.balance,
        "immature": account.immature,
        "bonded": account.bonded,
        "total": account.total,
        "nonce": account.nonce,
    }


def _transport(
    node: NodeService,
    *,
    faucet_status: int | None = None,
    mined_height: int = 20,
    settles_immediately: bool = False,
) -> httpx.MockTransport:
    """Route the generator's requests into the real node.

    ``GET /v1/accounts`` is answered from the mempool *projection* instead of the
    tip state so a cycle's grants and sends line up without mining a block: the
    block a real node settles between the two phases applies exactly these
    transactions. Every admission still goes through ``NodeService``.
    ``settles_immediately`` additionally reports an empty pool, standing in for a
    node that mines each cycle before the next one starts.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/mempool":
            view = node.mempool_view().model_dump(mode="json")
            if settles_immediately:
                view["transactions"] = []
            return httpx.Response(200, json=view)
        if path.startswith("/v1/accounts/"):
            address = path.removeprefix("/v1/accounts/")
            return httpx.Response(200, json=_account_payload(node, address))
        if path == "/v1/faucet":
            if faucet_status is not None:
                return httpx.Response(faucet_status, json={"error": "rate limited"})
            body = json.loads(request.content)
            view = node.faucet_request(body["address"], client="traffic", amount=body.get("amount"))
            return httpx.Response(200, json=view.model_dump(mode="json"))
        if path == "/v1/tx":
            tx = Transaction.model_validate(json.loads(request.content))
            accepted = node.submit_transaction(tx)  # a refusal fails the test here
            return httpx.Response(200, json=accepted.model_dump(mode="json"))
        if path.startswith("/v1/transactions/"):
            digest = path.removeprefix("/v1/transactions/")
            if digest not in node.mempool.transactions:
                return httpx.Response(404, json={"error": f"unknown transaction {digest!r}"})
            return httpx.Response(
                200,
                json={
                    "txid": digest,
                    "network": TESTNET.network_id,
                    "type": TxType.TRANSFER.value,
                    "height": mined_height,
                },
            )
        raise AssertionError(f"unexpected request {request.url}")

    return httpx.MockTransport(handler)


def _admitted(node: NodeService) -> list[Transaction]:
    return [entry.tx for entry in node.mempool.transactions.values()]


def test_traffic_wallets_are_fixed_testnet_addresses() -> None:
    """The pool is deterministic, so a restart reseats the same wallets."""
    first = traffic.wallet(0)
    assert first.address == traffic.wallet(0).address
    assert traffic.wallet(1).address != first.address
    assert traffic.wallet_seed(0) != traffic.wallet_seed(1)
    assert len(traffic.wallet_seed(0)) == 32
    assert validate_address(first.address, TESTNET)
    assert first.address.startswith("sccpt1")
    with pytest.raises(ValueError):
        traffic.wallet_seed(-1)


def test_a_cycle_funds_three_wallets_and_spends_between_them(node: NodeService) -> None:
    """Every grant and every send is an ordinary node-admitted transaction."""
    pool: list[traffic.Wallet] = []
    with httpx.Client(transport=_transport(node), base_url="http://node") as client:
        report = traffic.run_cycle(
            client,
            cycle=1,
            pool=pool,
            rng=random.Random(7),
            wallet_count=3,
            transfer_count=4,
            options=traffic.Options(wait_seconds=0.0, poll_seconds=0.0),
        )

    assert len(pool) == 3
    assert (report.wallet_count, report.funded, report.transfers) == (3, 3, 4)
    assert (report.faucet_grants, report.operator_grants) == (3, 0)
    assert report.confirmed == 7

    faucet = _faucet_address()
    grants = [tx for tx in _admitted(node) if tx.sender == faucet]
    sends = [tx for tx in _admitted(node) if tx.sender != faucet]

    assert sorted(tx.nonce for tx in grants) == [0, 1, 2]
    assert {tx.payload["amount"] for tx in grants} == {coins_to_units(traffic.GRANT_COINS)}
    assert {tx.payload["to"] for tx in grants} == {item.address for item in pool}

    assert len(sends) == 4
    addresses = {item.address for item in pool}
    assert all(tx.type is TxType.TRANSFER for tx in sends)
    assert all(tx.fee == TESTNET.min_fee for tx in sends)
    assert {tx.sender for tx in sends} <= addresses
    assert all(tx.payload["to"] in addresses for tx in sends)
    assert all(tx.payload["to"] != tx.sender for tx in sends)
    assert all(
        0 < tx.payload["amount"] <= coins_to_units(traffic.MAX_TRANSFER_COINS) for tx in sends
    )

    # Four sends over three wallets: one wallet must have numbered two of them.
    per_sender: dict[str, list[int]] = {}
    for tx in sends:
        per_sender.setdefault(tx.sender, []).append(tx.nonce)
    assert max(len(nonces) for nonces in per_sender.values()) >= 2
    for nonces in per_sender.values():
        assert sorted(nonces) == list(range(len(nonces)))


def test_a_rate_limited_faucet_falls_back_to_the_published_key(
    node: NodeService,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The faucet allows five grants an hour, so a wider cycle signs its own grants."""
    with httpx.Client(
        transport=_transport(node, faucet_status=429), base_url="http://node"
    ) as client:
        report = traffic.run_cycle(
            client,
            cycle=1,
            pool=[],
            rng=random.Random(5),
            wallet_count=2,
            transfer_count=1,
            options=traffic.Options(wait_seconds=0.0, poll_seconds=0.0),
        )

    assert (report.faucet_grants, report.operator_grants) == (0, 2)
    assert "published faucet key" in capsys.readouterr().err
    grants = [tx for tx in _admitted(node) if tx.sender == _faucet_address()]
    assert sorted(tx.nonce for tx in grants) == [0, 1]


def test_run_waits_between_cycles_and_stops_on_request(
    node: NodeService,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The pause between cycles lands in the configured window."""
    pauses: list[float] = []
    with httpx.Client(
        transport=_transport(node, settles_immediately=True), base_url="http://node"
    ) as client:
        reports = traffic.run(
            client,
            options=traffic.Options(
                max_wallets=3,
                wait_seconds=0.0,
                poll_seconds=0.0,
                idle_min_seconds=60.0,
                idle_max_seconds=300.0,
            ),
            rng=random.Random(11),
            cycles=2,
            sleep=pauses.append,
        )

    assert len(reports) == 2
    assert len(pauses) == 1
    assert 60.0 <= pauses[0] <= 300.0
    assert "cycle 2:" in capsys.readouterr().out


def test_a_transaction_the_pool_forgot_is_not_reported_as_mined() -> None:
    """A dropped transfer has left the pool but no block holds it."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/mempool":
            return httpx.Response(200, json={"transactions": []})
        return httpx.Response(404, json={"error": "unknown transaction"})

    with httpx.Client(transport=httpx.MockTransport(handler), base_url="http://node") as client:
        assert (
            traffic.wait_for_transactions(client, ["ab" * 32], wait_seconds=0, poll_seconds=0)
            == set()
        )


def test_pending_txids_rejects_a_malformed_pool() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"transactions": "one"})

    with (
        httpx.Client(transport=httpx.MockTransport(handler), base_url="http://node") as client,
        pytest.raises(traffic.TrafficError),
    ):
        traffic.pending_txids(client)


def test_the_operator_key_path_numbers_grants_across_cycles(node: NodeService) -> None:
    """``--no-faucet-endpoint`` must not restart the faucet nonce at zero."""
    pool: list[traffic.Wallet] = []
    options = traffic.Options(wait_seconds=0.0, poll_seconds=0.0, use_faucet_endpoint=False)
    with httpx.Client(
        transport=_transport(node, settles_immediately=True), base_url="http://node"
    ) as client:
        first = traffic.run_cycle(
            client,
            cycle=1,
            pool=pool,
            rng=random.Random(1),
            wallet_count=2,
            transfer_count=1,
            options=options,
        )
        second = traffic.run_cycle(
            client,
            cycle=2,
            pool=pool,
            rng=random.Random(2),
            wallet_count=3,
            transfer_count=1,
            options=options,
        )

    assert first.operator_grants == 2
    assert second.operator_grants == 1  # wallet 3 is new, so only it needs funding
    grants = [tx for tx in _admitted(node) if tx.sender == _faucet_address()]
    assert sorted(tx.nonce for tx in grants) == [0, 1, 2]


def test_the_generator_speaks_the_node_apps_own_routes(node: NodeService) -> None:
    """The paths and bodies the generator builds are the public API's own."""
    faucet_key = faucet_keypair(TESTNET.network_id)
    faucet_wallet = traffic.Wallet(
        index=-1,
        keypair=faucet_key,
        address=encode_address(faucet_key.public_key_bytes, TESTNET),
    )
    recipient = traffic.wallet(0).address

    with client_for(create_node_app(node)) as client:
        assert traffic.pending_txids(client) == set()
        assert traffic.balance(client, recipient) == 0

        tx = traffic.transfer_transaction(
            faucet_wallet,
            to=recipient,
            amount=coins_to_units(1),
            nonce=traffic.nonce(client, faucet_wallet.address),
        )
        digest = traffic.submit_transaction(client, tx)
        assert digest in traffic.pending_txids(client)

        # The faucet route numbers its own transfer, so this one lands on nonce 1.
        grant = traffic.claim_from_faucet(client, recipient, traffic.GRANT_COINS)
        assert {digest, grant} == traffic.pending_txids(client)

        record = node.transaction(grant)
        assert record.in_mempool
        assert record.height is None
        assert traffic.wait_for_transactions(client, [grant], wait_seconds=0) == set()


def test_the_command_line_refuses_a_pool_it_cannot_seat() -> None:
    """Two wallets is the floor: a transfer needs a distinct recipient."""
    with pytest.raises(SystemExit):
        traffic.main(["--max-wallets", "1"])
