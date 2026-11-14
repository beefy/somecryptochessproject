"""The synthetic traffic generator that keeps the hosted testnet in use."""

from __future__ import annotations

import json
import random
from collections.abc import Iterator, Sequence

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
    pool_noise: Sequence[str] = (),
    pending_recipients: Sequence[str] = (),
) -> httpx.MockTransport:
    """Route the generator's requests into the real node.

    ``GET /v1/accounts`` is answered from the mempool *projection* instead of the
    tip state so a cycle's grants and sends line up without mining a block: the
    block a real node settles between the two phases applies exactly these
    transactions. Every admission still goes through ``NodeService``.
    ``settles_immediately`` additionally reports an empty pool, standing in for a
    node that mines each cycle before the next one starts.
    ``pending_recipients`` are the wallets whose transfer stays in the pool, which
    is how a node behaves when a block does not carry the grant this time round.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/mempool":
            view = node.mempool_view().model_dump(mode="json")
            if settles_immediately:
                view["transactions"] = []
            view["transactions"] = [*view["transactions"], *pool_noise]
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
            entry = node.mempool.transactions[digest]
            recipient = (entry.tx.payload or {}).get("to")
            stuck = entry.tx.sender == _faucet_address() and recipient in pending_recipients
            return httpx.Response(
                200,
                json={
                    "txid": digest,
                    "network": TESTNET.network_id,
                    "type": TxType.TRANSFER.value,
                    "height": None if stuck else mined_height,
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
    pool = [traffic.wallet(index) for index in range(3)]
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


def test_run_seats_and_funds_the_whole_pool_up_front(
    node: NodeService,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Every wallet exists from the first cycle, rather than growing over time."""
    with httpx.Client(
        transport=_transport(node, settles_immediately=True), base_url="http://node"
    ) as client:
        reports = traffic.run(
            client,
            options=traffic.Options(
                max_wallets=4,
                wait_seconds=0.0,
                poll_seconds=0.0,
                idle_min_seconds=1.0,
                idle_max_seconds=1.0,
            ),
            rng=random.Random(3),
            cycles=1,
            sleep=lambda _: None,
        )

    assert [(report.wallet_count, report.funded) for report in reports] == [(4, 4)]
    grants = [tx for tx in _admitted(node) if tx.sender == _faucet_address()]
    assert len({tx.payload["to"] for tx in grants}) == 4
    assert "4 wallets" in capsys.readouterr().out


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
            pool=[traffic.wallet(index) for index in range(2)],
            rng=random.Random(5),
            wallet_count=2,
            transfer_count=1,
            options=traffic.Options(wait_seconds=0.0, poll_seconds=0.0),
        )

    assert (report.faucet_grants, report.operator_grants) == (0, 2)
    assert "published faucet key" in capsys.readouterr().err
    grants = [tx for tx in _admitted(node) if tx.sender == _faucet_address()]
    assert sorted(tx.nonce for tx in grants) == [0, 1]


def test_a_busy_mempool_does_not_stall_a_cycle(node: NodeService) -> None:
    """The drip service keeps the pool busy, so a cycle only waits on its own work."""
    with httpx.Client(
        transport=_transport(node, pool_noise=["ab" * 32]), base_url="http://node"
    ) as client:
        report = traffic.run_cycle(
            client,
            cycle=1,
            pool=[traffic.wallet(index) for index in range(2)],
            rng=random.Random(9),
            wallet_count=2,
            transfer_count=2,
            options=traffic.Options(wait_seconds=0.0, poll_seconds=0.0),
        )

    assert (report.funded, report.transfers, report.confirmed) == (2, 2, 4)


def test_a_cycle_with_an_unconfirmed_grant_still_sends(node: NodeService) -> None:
    """Funded wallets are spent even when one grant is still in the pool.

    This is the fresh-deploy case: the faucet's five-grant budget funds half the
    pool, the key path funds the rest, one of those grants misses the block, and
    the cycle used to return before sending anything -- five funded wallets and a
    transfer graph with nothing but faucet edges.
    """
    pool = [traffic.wallet(index) for index in range(4)]
    with httpx.Client(
        transport=_transport(node, pending_recipients=[pool[-1].address]),
        base_url="http://node",
    ) as client:
        report = traffic.run_cycle(
            client,
            cycle=1,
            pool=pool,
            rng=random.Random(4),
            wallet_count=4,
            transfer_count=3,
            options=traffic.Options(wait_seconds=0.0, poll_seconds=0.0),
        )

    assert (report.funded, report.unconfirmed, report.transfers) == (4, 1, 3)
    assert report.confirmed == 6  # three grants and three sends mined
    wallets = {item.address for item in pool}
    assert [tx for tx in _admitted(node) if tx.sender in wallets], "no wallet paid another"


def test_the_operator_key_path_retries_a_nonce_the_pool_already_holds(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The drip spends the same faucet account, so a grant can collide with it.

    ``GET /v1/accounts`` reports the committed nonce, and the drip's pending
    transfer is not in it until a block takes it. The grant is refused, the block
    lands, and the retry renumbers instead of failing the cycle.
    """
    attempts: list[int] = []
    state = {"conflicts": 1, "committed": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/v1/accounts/"):
            unsigned = {"network": TESTNET.network_id, "address": _faucet_address()}
            return httpx.Response(
                200,
                json={
                    **unsigned,
                    "balance": coins_to_units(1000),
                    "immature": 0,
                    "bonded": 0,
                    "total": coins_to_units(1000),
                    "nonce": state["committed"],
                },
            )
        if path == "/v1/tx":
            tx = Transaction.model_validate(json.loads(request.content))
            attempts.append(tx.nonce)
            if state["conflicts"]:
                state["conflicts"] -= 1
                state["committed"] = tx.nonce + 1  # the block that took the drip's transfer
                return httpx.Response(
                    400,
                    json={
                        "detail": f"{tx.sender} already has a pending transaction "
                        f"with nonce {tx.nonce}"
                    },
                )
            return httpx.Response(200, json={"network": TESTNET.network_id, "txid": "ab" * 32})
        raise AssertionError(f"unexpected request {request.url}")

    with httpx.Client(transport=httpx.MockTransport(handler), base_url="http://node") as client:
        gateway = traffic.FaucetGateway(client, use_faucet_endpoint=False, poll_seconds=0.0)
        gateway.start_cycle()
        digest, via_endpoint = gateway.grant(traffic.wallet(0).address, traffic.GRANT_COINS)

    assert attempts == [0, 1]
    assert (digest, via_endpoint) == ("ab" * 32, False)
    assert "waiting for the block that frees the nonce" in capsys.readouterr().err


def test_a_nonce_conflict_that_never_clears_is_reported() -> None:
    """Waiting is bounded: a nonce the chain never frees raises instead of hanging."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/v1/accounts/"):
            return httpx.Response(
                200,
                json={
                    "network": TESTNET.network_id,
                    "address": _faucet_address(),
                    "balance": coins_to_units(1000),
                    "immature": 0,
                    "bonded": 0,
                    "total": coins_to_units(1000),
                    "nonce": 0,
                },
            )
        return httpx.Response(
            400,
            json={"detail": f"{_faucet_address()} already has a pending transaction with nonce 0"},
        )

    with (
        httpx.Client(transport=httpx.MockTransport(handler), base_url="http://node") as client,
        pytest.raises(traffic.NonceConflict),
    ):
        gateway = traffic.FaucetGateway(client, use_faucet_endpoint=False, wait_seconds=0.0)
        gateway.start_cycle()
        gateway.grant(traffic.wallet(0).address, traffic.GRANT_COINS)


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
    pool = [traffic.wallet(index) for index in range(3)]
    options = traffic.Options(
        wait_seconds=0.0,
        poll_seconds=0.0,
        use_faucet_endpoint=False,
        # Every wallet reads as short, so each cycle funds the ones it seats.
        min_balance_coins=100,
    )
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
            wallet_count=2,
            transfer_count=1,
            options=options,
        )

    assert (first.operator_grants, first.funded) == (3, 3)  # cycle 1 seats the whole pool
    assert (second.operator_grants, second.funded) == (2, 2)
    grants = [tx for tx in _admitted(node) if tx.sender == _faucet_address()]
    assert sorted(tx.nonce for tx in grants) == [0, 1, 2, 3, 4]


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
