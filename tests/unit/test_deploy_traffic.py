"""The synthetic traffic generator that keeps the hosted testnet in use."""

from __future__ import annotations

import json
import random
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from deploy import synthetic_miner_creation as miners
from deploy import synthetic_testnet_traffic as traffic

from sccp.chain import dex
from sccp.chain.genesis import faucet_keypair
from sccp.chain.state import Pool
from sccp.chain.tx import htlc_id
from sccp.config import CHESS960_ASSET_ID, TESTNET, coins_to_units
from sccp.crypto.address import encode_address, validate_address
from sccp.errors import ConfigError
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


def _quiet_options(**overrides: object) -> traffic.Options:
    """Pacing for a unit test with the optional bridge and DEX traffic off.

    The side paths have their own tests; wallet tests count exactly the grants and
    sends they asked for, so the extras stay disabled unless a test opts in.
    """
    values: dict[str, object] = {
        "wait_seconds": 0.0,
        "poll_seconds": 0.0,
        "bridge": False,
        "dex": False,
    }
    values.update(overrides)
    return traffic.Options(**values)  # type: ignore[arg-type]


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
        "assets": dict(account.assets),
    }


def _transport(
    node: NodeService,
    *,
    faucet_status: int | None = None,
    mined_height: int = 20,
    settles_immediately: bool = False,
    account_source: str = "projection",
    pool_noise: Sequence[str] = (),
    pending_recipients: Sequence[str] = (),
    status_height: int | Callable[[], int] = 0,
) -> httpx.MockTransport:
    """Route the generator's requests into the real node.

    ``GET /v1/accounts`` is answered from the mempool *projection* instead of the
    tip state so a cycle's grants and sends line up without mining a block: the
    block a real node settles between the two phases applies exactly these
    transactions. Every admission still goes through ``NodeService``.
    ``settles_immediately`` additionally reports an empty pool, standing in for a
    node that mines each cycle before the next one starts.
    ``account_source`` chooses whether account views come from the mempool
    projection (what a node settles between phases) or the tip state (what a
    node without mining reports while transactions are still pending).
    ``pending_recipients`` are the wallets whose transfer stays in the pool, which
    is how a node behaves when a block does not carry the grant this time round.
    ``status_height`` is what ``GET /v1/status`` reports, either a fixed height or
    a callable a test can advance between polls.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/status":
            height = status_height() if callable(status_height) else status_height
            return httpx.Response(200, json={"network": TESTNET.network_id, "height": height})
        if path == "/v1/mempool":
            view = node.mempool_view().model_dump(mode="json")
            if settles_immediately:
                view["transactions"] = []
            view["transactions"] = [*view["transactions"], *pool_noise]
            return httpx.Response(200, json=view)
        if path == "/v1/pools":
            return httpx.Response(200, json=node.pools().model_dump(mode="json"))
        if path.startswith("/v1/accounts/"):
            address = path.removeprefix("/v1/accounts/")
            if account_source == "tip":
                state = node.chain.state
                account = state.account(address)
                body: dict[str, object] = {
                    "network": TESTNET.network_id,
                    "address": address,
                    "balance": account.balance,
                    "immature": account.immature,
                    "bonded": account.bonded,
                    "total": account.total,
                    "nonce": account.nonce,
                    "assets": dict(account.assets),
                }
                return httpx.Response(200, json=body)
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


def _seed_accounts(
    node: NodeService,
    addresses: Sequence[str],
    *,
    native_coins: int,
    variant_coins: int = 0,
) -> None:
    """Credit balances in both the tip and the cycle's projected state.

    The transport answers accounts from the projection, while the node validates
    against it too; seeding both is what a block between two cycles would leave.
    """
    for state in (node.chain.state, node.mempool.projection):
        for address in addresses:
            account = state.ensure(address)
            account.balance = coins_to_units(native_coins)
            if variant_coins:
                account.assets[CHESS960_ASSET_ID] = coins_to_units(variant_coins)


def _seed_pool(
    node: NodeService,
    *,
    holder: str,
    native_coins: int = 1_000,
    variant_coins: int = 1_000,
    share_coins: int = 1_000,
) -> str:
    """Seat an SCCP/Chess960 pool with ``holder`` owning every LP share."""
    pool_id = dex.pool_id(dex.NATIVE_ASSET, CHESS960_ASSET_ID)
    shares = coins_to_units(share_coins)
    for state in (node.chain.state, node.mempool.projection):
        state.pools[pool_id] = Pool(
            pool_id=pool_id,
            asset_a=dex.NATIVE_ASSET,
            asset_b=CHESS960_ASSET_ID,
            reserve_a=coins_to_units(native_coins),
            reserve_b=coins_to_units(variant_coins),
            total_shares=shares,
            shares={holder: shares},
        )
    return pool_id


def test_traffic_wallets_are_fixed_testnet_addresses() -> None:
    """The pool is deterministic, so a restart reseats the same wallets."""
    first = traffic.wallet(0)
    assert first.address == traffic.wallet(0).address
    assert traffic.wallet(1).address != first.address
    assert traffic.wallet_seed(0) != traffic.wallet_seed(1)
    assert len(traffic.wallet_seed(0)) == 32
    assert validate_address(first.address, TESTNET)
    assert first.address.startswith("sccpt1")
    with pytest.raises(ConfigError):
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
            options=_quiet_options(),
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
            options=_quiet_options(
                max_wallets=4,
                idle_min_seconds=1.0,
                idle_max_seconds=1.0,
            ),
            rng=random.Random(3),
            cycles=1,
            sleep=lambda _: None,
        )

    assert [(report.wallet_count, report.funded) for report in reports] == [(4, 4)]
    grants = [tx for tx in _admitted(node) if tx.sender == _faucet_address()]
    recipients = {tx.payload["to"] for tx in grants}
    pool_addresses = {traffic.wallet(index).address for index in range(4)}
    assert pool_addresses <= recipients
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
            options=_quiet_options(),
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
            options=_quiet_options(),
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
            options=_quiet_options(),
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
            options=_quiet_options(
                max_wallets=3,
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


def test_a_burst_queues_the_whole_spike_before_waiting(node: NodeService) -> None:
    """A burst tops the pool up, sends in waves and waits only once at the end."""
    pauses: list[float] = []
    with httpx.Client(
        transport=_transport(node, settles_immediately=True), base_url="http://node"
    ) as client:
        report = traffic.run_burst(
            client,
            after_cycle=4,
            pool=[traffic.wallet(index) for index in range(3)],
            rng=random.Random(7),
            options=_quiet_options(
                max_transfer_coins=1,
                burst_transfers=18,
                burst_rounds=3,
                burst_interval_seconds=0.5,
            ),
            sleep=pauses.append,
        )

    assert report.burst is True
    assert (report.cycle, report.wallet_count, report.funded) == (4, 3, 3)
    assert report.transfers == 18
    assert report.confirmed == 21  # three grants and eighteen sends
    assert pauses == [0.5, 0.5]

    # The spike arrives in waves, so more than one wallet sends.
    transfers = [tx for tx in _admitted(node) if tx.sender != _faucet_address()]
    assert len(transfers) == 18
    assert len({tx.sender for tx in transfers}) >= 2


def test_a_burst_keeps_its_nonces_across_waves_when_no_block_lands(
    node: NodeService,
) -> None:
    """A node's account view stays on the tip, so a later wave must reuse its own nonces.

    The real node reports ``nonce`` from the tip state, not the mempool: without
    the burst's booked nonces, wave two would resubmit wave one's and the node
    would refuse every send in it.
    """
    pool = [traffic.wallet(index) for index in range(3)]
    _seed_accounts(node, [wallet.address for wallet in pool], native_coins=100)
    with httpx.Client(
        transport=_transport(node, settles_immediately=True, account_source="tip"),
        base_url="http://node",
    ) as client:
        report = traffic.run_burst(
            client,
            after_cycle=1,
            pool=pool,
            rng=random.Random(23),
            options=_quiet_options(
                max_transfer_coins=1,
                burst_transfers=18,
                burst_rounds=3,
                burst_interval_seconds=0.0,
                burst_min_balance_coins=1,
            ),
        )

    assert report.funded == 0
    assert report.transfers == 18
    sends = [tx for tx in _admitted(node) if tx.sender != _faucet_address()]
    assert len(sends) == 18
    per_sender: dict[str, list[int]] = {}
    for tx in sends:
        per_sender.setdefault(tx.sender, []).append(tx.nonce)
    for nonces in per_sender.values():
        assert sorted(nonces) == list(range(len(nonces)))


def test_run_fires_a_burst_only_on_the_configured_interval(
    node: NodeService,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Ordinary cycles keep their pacing; the burst is an extra report."""
    pauses: list[float] = []
    with httpx.Client(
        transport=_transport(node, settles_immediately=True), base_url="http://node"
    ) as client:
        reports = traffic.run(
            client,
            options=_quiet_options(
                max_wallets=3,
                max_transfer_coins=1,
                idle_min_seconds=1.0,
                idle_max_seconds=1.0,
                burst_every=2,
                burst_rounds=1,
                burst_transfers=2,
                burst_interval_seconds=0.0,
            ),
            rng=random.Random(13),
            cycles=2,
            sleep=pauses.append,
        )

    assert [report.burst for report in reports] == [False, False, True]
    assert [report.cycle for report in reports] == [1, 2, 2]
    assert pauses == [1.0]  # one idle between the cycles, no pause in a one-wave burst
    assert "burst 2:" in capsys.readouterr().out


def test_a_burst_does_not_overrun_the_pool_budget(node: NodeService) -> None:
    """A grant is one top-up, so balances cap a burst instead of the flags lying."""
    with httpx.Client(
        transport=_transport(node, settles_immediately=True), base_url="http://node"
    ) as client:
        report = traffic.run_burst(
            client,
            after_cycle=1,
            pool=[traffic.wallet(index) for index in range(2)],
            rng=random.Random(17),
            options=_quiet_options(
                max_transfer_coins=5,
                burst_grant_coins=10,
                burst_min_balance_coins=9,
                burst_transfers=100,
                burst_rounds=2,
            ),
            sleep=lambda _: None,
        )

    # Ten coins cannot fund a hundred sends of one to five coins plus fees.
    assert 0 < report.transfers < 100
    assert len([tx for tx in _admitted(node) if tx.sender != _faucet_address()]) == report.transfers


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
    options = _quiet_options(
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


# -- the churn ----------------------------------------------------------------


def _offline_client() -> httpx.Client:
    """A client that fails the test rather than reaching for a host."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected request {request.url}")

    return httpx.Client(transport=httpx.MockTransport(handler), base_url="http://node")


@dataclass
class _RecordingFleet:
    """A churn that logs how much of the cycle had landed when it ran."""

    seen: dict[str, object]
    admitted: Callable[[], int]
    started: tuple[str, ...] = ("synthetic-miner-1",)
    closed: int = 0

    def announce(self) -> None:
        self.seen["announce"] = self.admitted()

    def churn(self, rng: random.Random, *, grant: object = None) -> miners.ChurnReport:
        self.seen["churn"] = self.admitted()
        self.seen["grant"] = grant
        return miners.ChurnReport(started=self.started, online=self.started)

    def close(self) -> None:
        self.closed += 1


def test_extras_are_off_unless_the_options_ask_for_them(monkeypatch: pytest.MonkeyPatch) -> None:
    """A plain traffic deployment must not measure the host or start a miner."""

    def measured(**kwargs: object) -> miners.HostCapacity:
        raise AssertionError("the host was measured although no churn was asked for")

    monkeypatch.setattr(miners, "host_capacity", measured)
    with _offline_client() as client:
        quiet = traffic.extras_for(client, options=traffic.Options(extra_miners=0))
    assert quiet.fleet is None
    quiet.close()  # nothing was started, so closing is a no-op rather than a crash


def test_the_churn_seats_the_pool_the_options_ask_for() -> None:
    """The fleet is built from the options, and seats only what the host allows."""
    capacity = miners.HostCapacity(
        cpu_count=8, available_memory_mb=4096, allowed=2, reason="two cores free"
    )
    options = traffic.Options(
        extra_miners=3,
        max_extra_online=1,
        relay="http://relay.example:8646",
        node="http://node.example:8645",
    )
    with _offline_client() as client:
        extras = traffic.extras_for(client, options=options, capacity=capacity)
    try:
        fleet = extras.fleet
        assert isinstance(fleet, miners.ExtraMinerFleet)
        assert fleet.directory == traffic.EXTRA_MINER_DIRECTORY
        assert fleet.capacity is capacity
        assert (fleet.relay, fleet.node) == (
            "http://relay.example:8646",
            "http://node.example:8645",
        )
        assert (fleet.pool_size, fleet.max_online) == (3, 1)
        assert len(fleet.pool) == 3
        assert fleet.online_limit == 1
        assert fleet.status_path is None  # only ``run`` asks for the deploy report
    finally:
        extras.close()


def test_the_churn_publishes_where_the_caller_asks(tmp_path: Path) -> None:
    """The fleet writes its report only where it is told to."""
    capacity = miners.HostCapacity(
        cpu_count=8, available_memory_mb=4096, allowed=1, reason="one core free"
    )
    report = tmp_path / "synthetic-fleet.json"
    with _offline_client() as client:
        extras = traffic.extras_for(
            client,
            options=traffic.Options(extra_miners=1),
            capacity=capacity,
            status_path=report,
        )
    try:
        assert extras.fleet is not None
        assert extras.fleet.status_path == report
    finally:
        extras.close()


def test_the_churn_measures_the_host_for_the_pool_it_was_asked_for(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``extras_for`` measures only when the caller did not supply a budget."""
    measured: list[dict[str, int]] = []

    def host(**kwargs: int) -> miners.HostCapacity:
        measured.append(kwargs)
        return miners.HostCapacity(
            cpu_count=8, available_memory_mb=4096, allowed=1, reason="one core free"
        )

    monkeypatch.setattr(miners, "host_capacity", host)
    with _offline_client() as client:
        extras = traffic.extras_for(client, options=traffic.Options(extra_miners=2))
    try:
        assert measured == [{"max_extra": 2}]
        assert extras.fleet is not None
        assert extras.fleet.capacity.allowed == 1
    finally:
        extras.close()


def test_a_cycle_churns_before_the_grants(node: NodeService) -> None:
    """Ordering is the contract: the churn sees an empty pool."""
    pool = [traffic.wallet(index) for index in range(3)]
    seen: dict[str, object] = {}
    with httpx.Client(transport=_transport(node), base_url="http://node") as client:
        admitted = _admitted_count(node)
        report = traffic.run_cycle(
            client,
            cycle=1,
            pool=pool,
            rng=random.Random(11),
            wallet_count=3,
            transfer_count=2,
            options=_quiet_options(),
            extras=traffic.Extras(
                fleet=_RecordingFleet(seen=seen, admitted=admitted),
            ),
        )

    assert seen["churn"] == 0, "the churn ran after a grant was already in the pool"
    assert callable(seen["grant"]), "a churn that cannot fund a seat is refused by the node"
    assert report.miners == "miners: started synthetic-miner-1; online: synthetic-miner-1"
    summary = traffic._summary(report)
    assert "| miners: started synthetic-miner-1; online: synthetic-miner-1" in summary


def test_a_cycle_locks_and_claims_a_native_htlc(node: NodeService) -> None:
    """Odd cycles bridge: lock from one wallet, claim from the beneficiary."""
    pool = [traffic.wallet(index) for index in range(3)]
    _seed_accounts(node, [item.address for item in pool], native_coins=50)
    with httpx.Client(transport=_transport(node), base_url="http://node") as client:
        report = traffic.run_cycle(
            client,
            cycle=1,
            pool=pool,
            rng=random.Random(5),
            wallet_count=3,
            transfer_count=1,
            options=_quiet_options(bridge=True, bridge_coins=2, min_balance_coins=100),
        )

    assert "locked 2.00000000 SCCP" in report.bridge
    assert "claimed htlc" in report.bridge
    assert report.side_unconfirmed == 0
    assert report.confirmed == 6  # three grants, one send, the lock and the claim

    admitted = _admitted(node)
    locks = [tx for tx in admitted if tx.type is TxType.HTLC_LOCK]
    claims = [tx for tx in admitted if tx.type is TxType.HTLC_CLAIM]
    assert len(locks) == len(claims) == 1
    lock, claim = locks[0], claims[0]
    assert lock.payload["amount"] == coins_to_units(2)
    assert lock.payload["timelock_height"] > 1
    assert claim.payload["htlc_id"] == htlc_id(lock.sender, lock.nonce)
    assert lock.payload["beneficiary"] == claim.sender
    assert f"| {report.bridge}" in traffic._summary(report)


def test_an_even_cycle_reports_a_refund_that_must_wait_for_its_height(
    node: NodeService,
) -> None:
    """A refund is only sent once the tip reaches the timelock; the wait is bounded."""
    pool = [traffic.wallet(index) for index in range(3)]
    _seed_accounts(node, [item.address for item in pool], native_coins=50)
    with httpx.Client(
        transport=_transport(node, status_height=0), base_url="http://node"
    ) as client:
        report = traffic.run_cycle(
            client,
            cycle=2,
            pool=pool,
            rng=random.Random(6),
            wallet_count=3,
            transfer_count=1,
            options=_quiet_options(
                bridge=True,
                bridge_coins=2,
                bridge_refund_blocks=50,
                min_balance_coins=100,
            ),
        )

    assert "locked htlc" in report.bridge
    assert "refund waits for height" in report.bridge
    assert report.side_unconfirmed == 0
    admitted = _admitted(node)
    assert [tx for tx in admitted if tx.type is TxType.HTLC_LOCK]
    assert not [tx for tx in admitted if tx.type is TxType.HTLC_REFUND]


def test_an_even_cycle_refunds_once_the_timelock_is_reached(node: NodeService) -> None:
    """The height wait ends at the timelock and the refund is submitted by its sender."""
    pool = [traffic.wallet(index) for index in range(3)]
    _seed_accounts(node, [item.address for item in pool], native_coins=50)
    calls = 0

    def status_height() -> int:
        """Report the lock's block first, then the block that reaches its timelock.

        The generator only reads the tip; moving the pool's view of it forward is
        this test's stand-in for a miner settling the lock between the two legs.
        """
        nonlocal calls
        calls += 1
        if calls == 1:
            return 0
        node.mempool.tip_state.height = 1
        return 2

    with httpx.Client(
        transport=_transport(node, status_height=status_height),
        base_url="http://node",
    ) as client:
        report = traffic.run_cycle(
            client,
            cycle=2,
            pool=pool,
            rng=random.Random(12),
            wallet_count=3,
            transfer_count=1,
            options=_quiet_options(
                bridge=True,
                bridge_coins=2,
                bridge_refund_blocks=1,
                min_balance_coins=100,
                wait_seconds=1.0,
            ),
        )

    assert "refunded htlc" in report.bridge
    assert report.side_unconfirmed == 0
    admitted = _admitted(node)
    locks = [tx for tx in admitted if tx.type is TxType.HTLC_LOCK]
    refunds = [tx for tx in admitted if tx.type is TxType.HTLC_REFUND]
    assert len(locks) == len(refunds) == 1
    assert refunds[0].sender == locks[0].sender
    assert refunds[0].payload["htlc_id"] == htlc_id(locks[0].sender, locks[0].nonce)


def test_a_cycle_swaps_against_an_existing_pool(node: NodeService) -> None:
    """A seated pool is enough: the DEX leg needs nobody to hold LP shares."""
    pool = [traffic.wallet(index) for index in range(3)]
    _seed_accounts(node, [item.address for item in pool], native_coins=50, variant_coins=5)
    pool_id = _seed_pool(node, holder=_faucet_address())
    with httpx.Client(transport=_transport(node), base_url="http://node") as client:
        report = traffic.run_cycle(
            client,
            cycle=1,
            pool=pool,
            rng=random.Random(7),
            wallet_count=3,
            transfer_count=1,
            options=_quiet_options(bridge=False, dex=True, dex_coins=2, min_balance_coins=100),
        )

    assert report.dex.startswith("dex: swapped 2.00000000 SCCP")
    assert report.dex.endswith(f"in pool {pool_id[:12]}")
    assert report.side_unconfirmed == 0
    assert report.confirmed == 5  # three grants, one send and the swap
    swaps = [tx for tx in _admitted(node) if tx.type is TxType.SWAP]
    assert len(swaps) == 1
    assert swaps[0].payload["pool_id"] == pool_id
    assert swaps[0].payload["amount_in"] == coins_to_units(2)
    assert "asset_in" not in swaps[0].payload  # native SCCP is the omitted side
    assert f"| {report.dex}" in traffic._summary(report)


def test_a_cycle_adds_liquidity_on_its_rotation(node: NodeService) -> None:
    """Cycle two of the rotation deposits both pool sides in proportion."""
    pool = [traffic.wallet(index) for index in range(3)]
    _seed_accounts(node, [item.address for item in pool], native_coins=50, variant_coins=5)
    pool_id = _seed_pool(node, holder=_faucet_address())
    with httpx.Client(transport=_transport(node), base_url="http://node") as client:
        report = traffic.run_cycle(
            client,
            cycle=2,
            pool=pool,
            rng=random.Random(10),
            wallet_count=3,
            transfer_count=1,
            options=_quiet_options(bridge=False, dex=True, dex_coins=2, min_balance_coins=100),
        )

    assert report.dex.startswith("dex: added 2.00000000 SCCP + 2.00000000 C960")
    assert report.dex.endswith(f"to pool {pool_id[:12]}")
    assert report.side_unconfirmed == 0
    deposits = [tx for tx in _admitted(node) if tx.type is TxType.ADD_LIQUIDITY]
    assert len(deposits) == 1
    assert deposits[0].payload["pool_id"] == pool_id
    assert deposits[0].payload["amount_a"] == deposits[0].payload["amount_b"] == coins_to_units(2)


def test_a_cycle_removes_liquidity_on_its_rotation(node: NodeService) -> None:
    """Cycle three redeems a tenth of the sending wallet's LP shares."""
    pool = [traffic.wallet(index) for index in range(3)]
    _seed_accounts(node, [item.address for item in pool], native_coins=50, variant_coins=5)
    pool_id = _seed_pool(node, holder=pool[0].address)
    with httpx.Client(transport=_transport(node), base_url="http://node") as client:
        report = traffic.run_cycle(
            client,
            cycle=3,
            pool=pool,
            rng=random.Random(11),
            wallet_count=3,
            transfer_count=1,
            options=_quiet_options(bridge=False, dex=True, dex_coins=2, min_balance_coins=100),
        )

    assert report.dex.startswith(
        "dex: removed 10000000000 shares (100.00000000 SCCP + 100.00000000 C960)"
    )
    assert report.dex.endswith(f"from pool {pool_id[:12]}")
    assert report.side_unconfirmed == 0
    redemptions = [tx for tx in _admitted(node) if tx.type is TxType.REMOVE_LIQUIDITY]
    assert len(redemptions) == 1
    assert redemptions[0].sender == pool[0].address
    assert redemptions[0].payload["shares"] == coins_to_units(100)


def test_a_cycle_without_a_pool_creates_one_from_the_variant_asset(node: NodeService) -> None:
    """The first cycle seeds the network's SCCP/C960 market when none exists."""
    pool = [traffic.wallet(index) for index in range(2)]
    _seed_accounts(node, [item.address for item in pool], native_coins=50, variant_coins=5)
    # The testnet genesis now seeds this pair; remove it to exercise the
    # fallback a network without a genesis pool still needs.
    node.chain.state.pools.clear()
    node.mempool.projection.pools.clear()
    with httpx.Client(transport=_transport(node), base_url="http://node") as client:
        report = traffic.run_cycle(
            client,
            cycle=1,
            pool=pool,
            rng=random.Random(8),
            wallet_count=2,
            transfer_count=1,
            options=_quiet_options(bridge=False, dex=True, dex_coins=2, min_balance_coins=100),
        )

    assert "dex: created SCCP/C960 pool" in report.dex
    assert report.side_unconfirmed == 0
    creates = [tx for tx in _admitted(node) if tx.type is TxType.CREATE_POOL]
    assert len(creates) == 1
    assert creates[0].payload["asset_b"] == CHESS960_ASSET_ID
    assert creates[0].payload["amount_a"] == creates[0].payload["amount_b"] == coins_to_units(2)


def test_side_traffic_that_cannot_find_coins_is_reported_as_a_skip(node: NodeService) -> None:
    """An unfunded pool is a skipped DEX leg, never a failed cycle."""
    pool = [traffic.wallet(index) for index in range(2)]
    with httpx.Client(transport=_transport(node), base_url="http://node") as client:
        report = traffic.run_cycle(
            client,
            cycle=1,
            pool=pool,
            rng=random.Random(9),
            wallet_count=2,
            transfer_count=0,
            options=_quiet_options(bridge=False, dex=True, min_balance_coins=0),
        )

    pool_id = dex.pool_id(dex.NATIVE_ASSET, CHESS960_ASSET_ID)
    assert report.dex == f"dex: skipped (no wallet held both sides of pool {pool_id[:12]})"
    assert (report.transfers, report.side_unconfirmed) == (0, 0)
    assert not [tx for tx in _admitted(node) if tx.type is TxType.CREATE_POOL]


def _admitted_count(node: NodeService) -> Callable[[], int]:
    """The pool size the node's own mempool reports, read when the extras run."""
    return lambda: len(_admitted(node))


def test_run_stops_the_miners_it_was_given(node: NodeService) -> None:
    """A churn that outlived the generator would leave children nobody stops."""
    fleet = _RecordingFleet(seen={}, admitted=lambda: 0)
    options = _quiet_options(
        max_wallets=4,
        extra_miners=0,
    )
    with httpx.Client(transport=_transport(node), base_url="http://node") as client:
        reports = traffic.run(
            client,
            options=options,
            cycles=1,
            sleep=lambda seconds: None,
            extras=traffic.Extras(fleet=fleet),
        )

    assert len(reports) == 1
    assert fleet.closed == 1


def test_run_announces_the_fleet_before_the_first_cycle(node: NodeService) -> None:
    """The report must exist even if the first cycle never gets to seat a miner.

    A seat registers by transaction, so a chain that has stopped producing blocks
    freezes `start_all` before it publishes anything. Announcing first leaves the
    deploy a readable file -- and a phase that says why nothing is online yet.
    """
    fleet = _RecordingFleet(seen={}, admitted=lambda: 0)
    with httpx.Client(transport=_transport(node), base_url="http://node") as client:
        traffic.run(
            client,
            options=_quiet_options(max_wallets=4, extra_miners=0),
            cycles=1,
            sleep=lambda seconds: None,
            extras=traffic.Extras(fleet=fleet),
        )

    assert list(fleet.seen)[:2] == ["announce", "churn"], (
        "the fleet was announced after the cycle started"
    )


def test_run_builds_the_extras_from_its_options(
    node: NodeService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``run`` owns the extras when the caller passes none, and closes them either way."""
    built: list[traffic.Options] = []
    reported: list[Path | None] = []
    fleet = _RecordingFleet(seen={}, admitted=lambda: 0)

    def extras_for(
        client: httpx.Client,
        *,
        options: traffic.Options,
        params: object = TESTNET,
        capacity: miners.HostCapacity | None = None,
        status_path: Path | None = None,
    ) -> traffic.Extras:
        built.append(options)
        reported.append(status_path)
        return traffic.Extras(fleet=fleet)

    monkeypatch.setattr(traffic, "extras_for", extras_for)
    options = _quiet_options(
        max_wallets=4,
        extra_miners=1,
    )
    with httpx.Client(transport=_transport(node), base_url="http://node") as client:
        reports = traffic.run(client, options=options, cycles=1, sleep=lambda seconds: None)

    assert len(reports) == 1
    assert built == [options]
    # The seats publish what they hold where the deploy workflow looks for it:
    # they are children of this container, so nothing else can observe them.
    assert reported == [traffic.FLEET_STATUS_PATH]
    assert fleet.closed == 1


# -- the command line ---------------------------------------------------------


def _fake_run(monkeypatch: pytest.MonkeyPatch, captured: dict[str, object]) -> None:
    """Swap the generator's loop for one that records the options it was handed."""

    def fake_run(client: httpx.Client, **kwargs: object) -> list[traffic.CycleReport]:
        captured.update(kwargs)
        return []

    monkeypatch.setattr(traffic, "run", fake_run)


def test_the_command_line_carries_the_churn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--relay`` and the pool sizes reach the generator's options."""
    captured: dict[str, object] = {}
    _fake_run(monkeypatch, captured)
    code = traffic.main(
        [
            "--relay",
            "http://relay.example:8646",
            "--extra-miners",
            "3",
            "--max-extra-online",
            "1",
            "--cycles",
            "2",
        ]
    )

    assert code == 0
    options = captured["options"]
    assert isinstance(options, traffic.Options)
    assert options.relay == "http://relay.example:8646"
    assert (options.extra_miners, options.max_extra_online) == (3, 1)
    assert captured["cycles"] == 2


def test_the_stack_defaults_keep_the_churn_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Compose passes no churn flags, so the parser's defaults are the deployment."""
    captured: dict[str, object] = {}
    _fake_run(monkeypatch, captured)
    assert traffic.main([]) == 0

    options = captured["options"]
    assert isinstance(options, traffic.Options)
    assert options.relay == "http://relay:8646"
    assert options.node == "http://node:8645"
    assert options.extra_miners == traffic.EXTRA_MINERS >= 1
    assert options.max_extra_online == traffic.MAX_EXTRA_ONLINE >= 1
    assert options.bridge and options.dex
    assert options.bridge_coins == traffic.BRIDGE_COINS >= 1
    assert options.dex_coins == traffic.DEX_COINS >= 1


def test_the_command_line_carries_the_side_traffic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bridge and DEX knobs reach the generator, including the off switches."""
    captured: dict[str, object] = {}
    _fake_run(monkeypatch, captured)
    code = traffic.main(
        [
            "--bridge-coins",
            "3",
            "--dex-coins",
            "4",
            "--dex-asset",
            CHESS960_ASSET_ID,
        ]
    )

    assert code == 0
    options = captured["options"]
    assert isinstance(options, traffic.Options)
    assert (options.bridge, options.dex) == (True, True)
    assert (options.bridge_coins, options.dex_coins) == (3, 4)
    assert options.dex_asset == CHESS960_ASSET_ID

    assert traffic.main(["--no-bridge", "--no-dex"]) == 0
    options = captured["options"]
    assert isinstance(options, traffic.Options)
    assert (options.bridge, options.dex) == (False, False)


@pytest.mark.parametrize(
    "argument",
    [
        ["--extra-miners", "-1"],
        ["--max-extra-online", "0"],
        ["--bridge-coins", "0"],
        ["--dex-coins", "0"],
        ["--dex-asset", "not-an-asset"],
        ["--cycles", "0"],
    ],
)
def test_the_command_line_refuses_impossible_churn(argument: list[str]) -> None:
    with pytest.raises(SystemExit):
        traffic.main(argument)
