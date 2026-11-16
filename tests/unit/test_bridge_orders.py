"""The off-consensus signed order book behind the bridge UI."""

from __future__ import annotations

import pytest

from sccp.bridge.orders import (
    OrderBook,
    OrderError,
    order_id,
    sign_order,
    sign_take,
    take_id,
    verify_order,
)
from sccp.config import DEVNET
from sccp.crypto.address import encode_address
from sccp.crypto.pq import HybridKeypair, hybrid_keypair_from_seed
from sccp.errors import ProtocolError
from sccp.node.app import create_app
from sccp.node.service import NodeService
from sccp.serialization import Domain, domain_hex
from sccp.transport import client_for

COINS = 100_000_000
NOW = 1_700_000_000
ETH = "0x" + "12" * 20
TAKER_ETH = "0x" + "34" * 20
HASHLOCK = "ab" * 32
TX_HASH = "0x" + "56" * 32


def _key(seed: int) -> HybridKeypair:
    return hybrid_keypair_from_seed(bytes([seed]) * 32)


ALICE = _key(1)
BOB = _key(2)


def _order(
    *,
    keypair: HybridKeypair = ALICE,
    give: int = 10 * COINS,
    want: str = str(5 * 10**15),
    min_timelock: int = 50,
    expiry: int = NOW + 1_000,
    nonce: int = 0,
):
    return sign_order(
        keypair,
        DEVNET,
        maker_eth=ETH,
        give_amount=give,
        want_amount=want,
        min_sccp_timelock=min_timelock,
        expiry=expiry,
        nonce=nonce,
    )


def _take(order: str, *, keypair: HybridKeypair = BOB, height: int = 60, nonce: int = 0):
    return sign_take(
        keypair,
        DEVNET,
        order=order,
        taker_eth=TAKER_ETH,
        hashlock=HASHLOCK,
        eth_timelock=NOW + 3_600,
        sccp_timelock_height=height,
        eth_tx_hash=TX_HASH,
        nonce=nonce,
    )


def test_an_order_is_signed_over_its_canonical_body() -> None:
    order = _order()
    digest = order_id(order)
    assert digest == domain_hex(Domain.ORDER, order.body())
    assert order.maker_sccp == encode_address(ALICE.public_key_bytes, DEVNET)
    assert verify_order(order, DEVNET, NOW) == digest


def test_the_book_admits_lists_and_returns_an_order() -> None:
    book = OrderBook()
    order = _order()
    digest = book.add(order, DEVNET, NOW)
    assert book.get(digest, NOW) == order
    rows = book.list(NOW)
    assert [row[0] for row in rows] == [digest]
    assert book.list(NOW, maker_sccp="sccpt1nope") == ()
    assert book.list(NOW, give_asset="ETH") == ()


def test_a_tampered_order_is_rejected() -> None:
    order = _order()
    tampered = order.model_copy(update={"give_amount": order.give_amount + 1})
    with pytest.raises(OrderError, match="signature"):
        OrderBook().add(tampered, DEVNET, NOW)


def test_an_expired_order_is_not_admitted() -> None:
    with pytest.raises(OrderError, match="expired"):
        OrderBook().add(_order(expiry=NOW), DEVNET, NOW)


def test_a_take_attaches_once_and_must_respect_the_minimum_timelock() -> None:
    book = OrderBook()
    digest = book.add(_order(), DEVNET, NOW)
    low = _take(digest, height=49)
    with pytest.raises(OrderError, match="below the order's minimum"):
        book.take(low, DEVNET, NOW)

    take = _take(digest)
    admitted = book.take(take, DEVNET, NOW)
    assert admitted == take_id(take)
    assert book.take_for(digest) == take

    with pytest.raises(OrderError, match="already has a taker"):
        book.take(_take(digest, nonce=1), DEVNET, NOW)


def test_an_expired_order_with_a_take_stays_readable() -> None:
    book = OrderBook()
    digest = book.add(_order(expiry=NOW + 10), DEVNET, NOW)
    book.take(_take(digest), DEVNET, NOW)
    assert book.get(digest, NOW + 20) is not None
    assert book.take_for(digest) is not None
    assert book.list(NOW + 20)[0][0] == digest


def test_a_take_for_an_unknown_order_is_rejected() -> None:
    with pytest.raises(OrderError, match="unknown or expired order"):
        OrderBook().take(_take("11" * 32), DEVNET, NOW)


def test_a_tampered_take_is_rejected() -> None:
    book = OrderBook()
    digest = book.add(_order(), DEVNET, NOW)
    take = _take(digest)
    tampered = take.model_copy(update={"sccp_timelock_height": 999})
    with pytest.raises(OrderError, match="signature"):
        book.take(tampered, DEVNET, NOW)


def test_the_node_routes_publish_an_order_and_its_take() -> None:
    service = NodeService.open(DEVNET, clock=lambda: NOW)
    order = _order()
    try:
        with client_for(create_app(service)) as client:
            bridge = client.get("/v1/bridge").json()
            assert bridge["network"] == "devnet"
            assert bridge["eth_chain_id"] == 31337
            assert bridge["eth_chain_name"] == "Anvil devnet"

            accepted = client.post("/v1/orders", json=order.model_dump(mode="json"))
            assert accepted.status_code == 200, accepted.text
            accepted_view = accepted.json()
            assert accepted_view["order_id"] == order_id(order)

            listing = client.get("/v1/orders?maker_sccp=" + order.maker_sccp).json()
            assert [row["order_id"] for row in listing["orders"]] == [order_id(order)]

            take = _take(order_id(order))
            submitted = client.post(
                f"/v1/orders/{order_id(order)}/takes",
                json=take.model_dump(mode="json"),
            )
            assert submitted.status_code == 200, submitted.text
            assert submitted.json()["take_id"] == take_id(take)

            detail = client.get(f"/v1/orders/{order_id(order)}").json()
            assert detail["take"]["eth_tx_hash"] == TX_HASH

            assert client.get("/v1/htlcs").json()["htlcs"] == []
            missing = client.get("/v1/htlcs/" + "ab" * 32)
            assert missing.status_code == 404
    finally:
        service.close()


def test_the_node_refuses_an_sccp_timelock_that_outlives_the_eth_leg() -> None:
    service = NodeService.open(DEVNET, clock=lambda: NOW)
    try:
        order = _order()
        accepted = service.submit_order(order)
        unsafe = sign_take(
            BOB,
            DEVNET,
            order=accepted.order_id,
            taker_eth=TAKER_ETH,
            hashlock=HASHLOCK,
            eth_timelock=NOW + 100,
            sccp_timelock_height=200,
            eth_tx_hash=TX_HASH,
            nonce=0,
        )
        with pytest.raises(ProtocolError, match="safe maximum"):
            service.submit_take(accepted.order_id, unsafe)
    finally:
        service.close()
