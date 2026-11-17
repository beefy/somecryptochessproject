"""The off-consensus ETH/SCCP order book.

An order is a maker's signed promise to sell SCCP for ETH; a take is a taker's
signed announcement that the ETH leg is locked. Neither is consensus state: the
book holds no funds and can be mirrored, and losing it only costs a re-post. The
signature is the same hybrid scheme and canonical domain hash the ledger uses, so
an order cannot be forged or redirected to another beneficiary.

The node keeps the book in memory. Expiry is evaluated against the injected
clock, which keeps tests deterministic and means an operator never has to trust
a browser clock for admission.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Final, Literal

from pydantic import Field

from sccp.bridge.config import ADDRESS_PATTERN
from sccp.config import NetworkParams, get_network
from sccp.crypto.address import encode_address, validate_address
from sccp.crypto.pq import (
    HYBRID_PUBLIC_KEY_SIZE,
    HybridKeypair,
    hybrid_sign,
    hybrid_verify,
    is_hybrid_public_key_hex,
)
from sccp.errors import ProtocolError
from sccp.protocol.models import ProtocolModel
from sccp.serialization import Domain, domain_hex

DIGEST = re.compile(r"^[0-9a-f]{64}$")
WEI = re.compile(r"^[1-9][0-9]*$")
TX_HASH = re.compile(r"^0x[0-9a-fA-F]{64}$")
MAX_ORDERS: Final = 1_000
MAX_ORDERS_PER_MAKER: Final = 10
MAX_TAKES: Final = 100


class OrderError(ProtocolError):
    """A malformed, unauthenticated or stale bridge order."""


class OrderBody(ProtocolModel):
    """The signed part of a sell-SCCP order."""

    network: str
    maker_sccp: str
    maker_eth: str
    give_asset: Literal["SCCP"] = "SCCP"
    give_amount: int
    want_asset: Literal["ETH"] = "ETH"
    want_amount: str = Field(min_length=1, max_length=78)
    """Wei as a decimal string: 18 decimals exceed JavaScript's safe integer range."""
    min_sccp_timelock: int
    expiry: int
    nonce: int


class Order(OrderBody):
    """A signed order. ``public_key`` stays outside the body but inside the wire."""

    public_key: str
    signature: str

    def body(self) -> dict[str, object]:
        """The canonical object :func:`order_id` hashes."""
        return self.model_dump(mode="json", exclude={"public_key", "signature"})


class TakeBody(ProtocolModel):
    """The signed part of a taker's announcement that the ETH leg is locked."""

    network: str
    order_id: str
    taker_sccp: str
    taker_eth: str
    hashlock: str
    eth_timelock: int
    sccp_timelock_height: int
    eth_tx_hash: str
    nonce: int


class Take(TakeBody):
    """A signed take. One order is whole-order and therefore takes one taker."""

    public_key: str
    signature: str

    def body(self) -> dict[str, object]:
        """The canonical object :func:`take_id` hashes."""
        return self.model_dump(mode="json", exclude={"public_key", "signature"})


def _require_eth_address(value: str, label: str) -> None:
    if not ADDRESS_PATTERN.fullmatch(value):
        raise OrderError(f"{label} must be a 0x-prefixed 20-byte address")
    if int(value, 16) == 0:
        raise OrderError(f"{label} must not be the zero address")


def _order_body_dump(order: OrderBody) -> dict[str, object]:
    data = order.model_dump(mode="json")
    data.pop("public_key", None)
    data.pop("signature", None)
    return data


def _take_body_dump(take: TakeBody) -> dict[str, object]:
    data = take.model_dump(mode="json")
    data.pop("public_key", None)
    data.pop("signature", None)
    return data


def order_id(order: OrderBody) -> str:
    """``H("sccp/v1/order", body)``; the public name of one order."""
    return domain_hex(Domain.ORDER, _order_body_dump(order))


def take_id(take: TakeBody) -> str:
    """``H("sccp/v1/take", body)``; the public name of one take."""
    return domain_hex(Domain.TAKE, _take_body_dump(take))


def sccp_timelock_height(
    current_height: int,
    eth_timelock: int,
    now: int,
    target_block_seconds: int,
    margin: int,
    minimum: int,
) -> int:
    """The SCCP height a taker may name for an ETH lock that expires at ``eth_timelock``.

    The SCCP leg must expire before the ETH leg by ``margin`` blocks, so the
    maker still has time to claim ETH after a last-moment SCCP claim. The same
    rule is implemented in the browser and enforced again by the node.
    """
    if target_block_seconds <= 0:
        raise OrderError("target_block_seconds must be positive")
    if margin < 0:
        raise OrderError("sccp_margin must be non-negative")
    blocks_until_eth = max(0, (eth_timelock - now) // target_block_seconds)
    safe_blocks = blocks_until_eth - margin
    if safe_blocks < 1:
        raise OrderError(
            f"the ETH timelock is too short for {margin} SCCP blocks of margin; "
            "choose a longer lock"
        )
    height = current_height + safe_blocks
    if height < minimum:
        raise OrderError(
            f"the order asks for SCCP height {minimum}, "
            f"but this ETH timelock is only safe up to {height}"
        )
    return height


def sign_order(
    keypair: HybridKeypair,
    params: NetworkParams,
    *,
    maker_eth: str,
    give_amount: int,
    want_amount: str,
    min_sccp_timelock: int,
    expiry: int,
    nonce: int,
) -> Order:
    """Build and sign a sell-SCCP order with ``keypair``."""
    maker = encode_address(keypair.public_key_bytes, params)
    body = OrderBody(
        network=params.network_id,
        maker_sccp=maker,
        maker_eth=maker_eth,
        give_amount=give_amount,
        want_amount=want_amount,
        min_sccp_timelock=min_sccp_timelock,
        expiry=expiry,
        nonce=nonce,
    )
    digest = order_id(body)
    return Order(
        **body.model_dump(mode="json"),
        public_key=keypair.public_key_hex,
        signature=hybrid_sign(keypair, bytes.fromhex(digest)).hex(),
    )


def sign_take(
    keypair: HybridKeypair,
    params: NetworkParams,
    *,
    order: str,
    taker_eth: str,
    hashlock: str,
    eth_timelock: int,
    sccp_timelock_height: int,
    eth_tx_hash: str,
    nonce: int,
) -> Take:
    """Build and sign a take for ``order`` after the ETH leg is locked."""
    taker = encode_address(keypair.public_key_bytes, params)
    body = TakeBody(
        network=params.network_id,
        order_id=order,
        taker_sccp=taker,
        taker_eth=taker_eth,
        hashlock=hashlock,
        eth_timelock=eth_timelock,
        sccp_timelock_height=sccp_timelock_height,
        eth_tx_hash=eth_tx_hash,
        nonce=nonce,
    )
    digest = take_id(body)
    return Take(
        **body.model_dump(mode="json"),
        public_key=keypair.public_key_hex,
        signature=hybrid_sign(keypair, bytes.fromhex(digest)).hex(),
    )


def _require_key_and_signature(
    public_key: str,
    signature: str,
    address: str,
    digest: str,
    params: NetworkParams,
    label: str,
) -> None:
    """Check that ``signature`` is the hybrid key behind ``address`` over ``digest``."""
    if not is_hybrid_public_key_hex(public_key):
        raise OrderError(f"{label} public_key must be a {HYBRID_PUBLIC_KEY_SIZE}-byte hybrid key")
    try:
        raw_key = bytes.fromhex(public_key)
        raw_signature = bytes.fromhex(signature)
    except ValueError:
        raise OrderError(f"{label} key or signature is not hex") from None
    if encode_address(raw_key, params) != address:
        raise OrderError(f"{label} public_key does not derive maker_sccp")
    if not hybrid_verify(raw_key, bytes.fromhex(digest), raw_signature):
        raise OrderError(f"{label} signature does not cover its canonical body")


def verify_order(order: Order, params: NetworkParams, now: int) -> str:
    """Validate and return the order id, or raise :class:`OrderError`."""
    params = get_network(params)
    if order.network != params.network_id:
        raise OrderError(f"order is for {order.network!r}, this node is {params.network_id!r}")
    if not validate_address(order.maker_sccp, params):
        raise OrderError("maker_sccp is not an address on this network")
    _require_eth_address(order.maker_eth, "maker_eth")
    if order.give_amount <= 0:
        raise OrderError("order amounts must be positive")
    if not WEI.fullmatch(order.want_amount):
        raise OrderError("want_amount must be a positive decimal string of wei")
    if order.expiry <= now:
        raise OrderError("order has expired")
    if order.min_sccp_timelock < 0 or order.nonce < 0:
        raise OrderError("order timelock and nonce must be non-negative")
    digest = order_id(order)
    _require_key_and_signature(
        order.public_key, order.signature, order.maker_sccp, digest, params, "order"
    )
    return digest


def verify_take(take: Take, params: NetworkParams, now: int) -> str:
    """Validate and return the take id, or raise :class:`OrderError`."""
    params = get_network(params)
    if take.network != params.network_id:
        raise OrderError(f"take is for {take.network!r}, this node is {params.network_id!r}")
    if not validate_address(take.taker_sccp, params):
        raise OrderError("taker_sccp is not an address on this network")
    _require_eth_address(take.taker_eth, "taker_eth")
    if not DIGEST.fullmatch(take.order_id) or not DIGEST.fullmatch(take.hashlock):
        raise OrderError("take order_id and hashlock must be 32-byte lowercase hex")
    if not TX_HASH.fullmatch(take.eth_tx_hash):
        raise OrderError("take eth_tx_hash must be a 0x-prefixed 32-byte hash")
    if take.eth_timelock <= now:
        raise OrderError("take eth_timelock is already in the past")
    if take.sccp_timelock_height < 0 or take.nonce < 0:
        raise OrderError("take height and nonce must be non-negative")
    digest = take_id(take)
    _require_key_and_signature(
        take.public_key, take.signature, take.taker_sccp, digest, params, "take"
    )
    return digest


@dataclass(slots=True)
class OrderBook:
    """One node's in-memory order book and take announcements."""

    orders: dict[str, Order] = field(default_factory=dict)
    takes: dict[str, Take] = field(default_factory=dict)
    """``take_id -> Take``; each take names its order, and an order takes one."""

    def _prune(self, now: int) -> None:
        """Drop expired orders, but keep a taken order until its swap finishes.

        A take can be in flight when the order's advertising window ends. Deleting
        it then would make the maker's own order disappear mid-swap, so an order
        with a take stays readable; a fresh taker is still rejected because the
        take check runs before admission.
        """
        for digest, order in list(self.orders.items()):
            if order.expiry > now:
                continue
            if any(take.order_id == digest for take in self.takes.values()):
                continue
            del self.orders[digest]

    def add(self, order: Order, params: NetworkParams, now: int) -> str:
        """Admit a signed order, returning its id."""
        digest = verify_order(order, params, now)
        self._prune(now)
        if digest in self.orders:
            return digest
        if len(self.orders) >= MAX_ORDERS:
            raise OrderError("the order book is full")
        mine = sum(
            1
            for candidate in self.orders.values()
            if candidate.maker_sccp == order.maker_sccp and candidate.expiry > now
        )
        if mine >= MAX_ORDERS_PER_MAKER:
            raise OrderError(f"{order.maker_sccp} already has {MAX_ORDERS_PER_MAKER} open orders")
        self.orders[digest] = order
        return digest

    def get(self, digest: str, now: int) -> Order | None:
        """One order by id, or ``None`` after expiry."""
        self._prune(now)
        return self.orders.get(digest)

    def list(
        self,
        now: int,
        *,
        maker_sccp: str | None = None,
        give_asset: str | None = None,
        want_asset: str | None = None,
    ) -> tuple[tuple[str, Order], ...]:
        """Open orders, best-priced SCCP first and then oldest first."""
        self._prune(now)
        rows = [
            (digest, order)
            for digest, order in self.orders.items()
            if (maker_sccp is None or order.maker_sccp == maker_sccp)
            and (give_asset is None or order.give_asset == give_asset)
            and (want_asset is None or order.want_asset == want_asset)
        ]
        rows.sort(
            key=lambda row: (
                int(row[1].want_amount) / row[1].give_amount,
                row[1].expiry,
                row[0],
            )
        )
        return tuple(rows)

    def take_for(self, digest: str) -> Take | None:
        """The single take attached to an order, if any."""
        for take in self.takes.values():
            if take.order_id == digest:
                return take
        return None

    def take(self, take: Take, params: NetworkParams, now: int) -> str:
        """Admit a signed take after checking it against the live order."""
        digest = verify_take(take, params, now)
        self._prune(now)
        order = self.orders.get(take.order_id)
        if order is None:
            raise OrderError(f"unknown or expired order {take.order_id}")
        if self.take_for(take.order_id) is not None:
            raise OrderError("that order already has a taker")
        if take.sccp_timelock_height < order.min_sccp_timelock:
            raise OrderError(
                f"the SCCP timelock {take.sccp_timelock_height} is below the order's "
                f"minimum {order.min_sccp_timelock}"
            )
        if len(self.takes) >= MAX_TAKES:
            raise OrderError("too many takes are pending")
        self.takes[digest] = take
        return digest
