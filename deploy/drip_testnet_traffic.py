"""Keep a live testnet supplied with the ledger work every block needs.

A block carries either a proof or at least one transaction (``PROTOCOL.md``
12.2): one proof or one pending transaction buys exactly one block. An epoch is
five blocks and the only ledger work the stack generates on its own is the
miners' availability and reveal -- four transactions for a two-miner roster. The
fifth piece of work has to be a decisive game, because a draw settles nothing
and a proof from the epoch that just ended is not claimable until the next epoch
opens. An epoch with no decisive game therefore runs one block short and the
chain stops at the boundary, and it cannot restart: nothing builds a block
without work in the pool.

This script closes that gap from outside the node. Whenever the node reports an
empty transaction pool, it signs one ordinary transfer from the published
testnet faucet and submits it to the node's public ``POST /v1/tx``. It never
builds blocks, holds a roster slot, or touches the node's database, so the node
validates every drip transfer exactly like any wallet's. Because it only sends
into an empty pool there is never an earlier faucet transfer in flight, which is
why the account nonce it reads from ``GET /v1/accounts`` is always the right one.

Usage::

    python deploy/drip_testnet_traffic.py --node http://node:8645

Like the faucet, this is a testnet convenience: the faucet seed and the sink
address are published and the coins have no value.
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Mapping
from typing import Final

import httpx

from sccp.chain.genesis import faucet_keypair
from sccp.chain.tx import sign_transaction, txid
from sccp.config import TESTNET, NetworkParams, coins_to_units
from sccp.crypto.address import encode_address, validate_address
from sccp.crypto.keys import keypair_from_seed
from sccp.protocol.models import Transaction, TxType

DRIP_AMOUNT: Final = coins_to_units(1)
"""Units per drip transfer: one coin, well under a faucet grant."""

SINK_SEED: Final = b"\xd7" * 32
"""Seed of the published sink address the drip pays. It is a coin sink."""

POLL_SECONDS: Final = 2.0
"""How often to look at the pool; the node's own poll is of the same order."""


class DripError(RuntimeError):
    """The drip could not read the node or submit its transfer."""


def sink_address(params: NetworkParams = TESTNET) -> str:
    """The deterministic recipient of a drip transfer."""
    return encode_address(keypair_from_seed(SINK_SEED).public_key_bytes, params)


def drip_transaction(
    params: NetworkParams = TESTNET,
    *,
    nonce: int,
    to: str | None = None,
    amount: int = DRIP_AMOUNT,
) -> Transaction:
    """Sign one faucet-to-sink transfer, ready for ``POST /v1/tx``."""
    keypair = faucet_keypair(params.network_id)
    tx = Transaction(
        network=params.network_id,
        type=TxType.TRANSFER,
        sender=encode_address(keypair.public_key_bytes, params),
        nonce=nonce,
        fee=params.min_fee,
        payload={
            "to": sink_address(params) if to is None else to,
            "amount": amount,
            "public_key": keypair.public_key_hex,
        },
    )
    return sign_transaction(keypair, tx)


def pool_needs_work(status: Mapping[str, object]) -> bool:
    """Whether ``/v1/status`` reports no pending transaction for a block."""
    pool = status.get("mempool")
    if not isinstance(pool, Mapping):
        raise DripError("/v1/status did not report a mempool")
    pending = pool.get("transactions")
    if not isinstance(pending, int) or isinstance(pending, bool) or pending < 0:
        raise DripError("/v1/status reported an invalid mempool transaction count")
    return pending == 0


def _get_object(client: httpx.Client, path: str) -> Mapping[str, object]:
    """GET ``path`` and return a JSON object, or raise :class:`DripError`."""
    response = client.get(path)
    if response.status_code >= 300:
        raise DripError(f"GET {path} failed: HTTP {response.status_code} {response.text}")
    payload = response.json()
    if not isinstance(payload, Mapping):
        raise DripError(f"GET {path} did not return an object")
    return payload


def drip_round(
    client: httpx.Client,
    *,
    params: NetworkParams = TESTNET,
    to: str | None = None,
    amount: int = DRIP_AMOUNT,
) -> str | None:
    """Submit one transfer if the pool is empty; return its txid, else ``None``."""
    if not pool_needs_work(_get_object(client, "/v1/status")):
        return None
    sender = encode_address(faucet_keypair(params.network_id).public_key_bytes, params)
    account = _get_object(client, f"/v1/accounts/{sender}")
    nonce = account.get("nonce")
    balance = account.get("balance")
    if not isinstance(nonce, int) or isinstance(nonce, bool) or nonce < 0:
        raise DripError(f"/v1/accounts/{sender} reported an invalid nonce")
    if not isinstance(balance, int) or isinstance(balance, bool):
        raise DripError(f"/v1/accounts/{sender} reported an invalid balance")
    cost = amount + params.min_fee
    if balance < cost:
        raise DripError(f"the faucet holds {balance} units, a drip costs {cost}")
    tx = drip_transaction(params, nonce=nonce, to=to, amount=amount)
    response = client.post("/v1/tx", json=tx.model_dump(mode="json"))
    if response.status_code >= 300:
        raise DripError(
            f"the node refused the transfer: HTTP {response.status_code} {response.text}"
        )
    accepted = response.json()
    digest = accepted.get("txid") if isinstance(accepted, Mapping) else None
    return digest if isinstance(digest, str) else txid(tx)


def run(
    client: httpx.Client,
    *,
    params: NetworkParams = TESTNET,
    to: str | None = None,
    amount: int = DRIP_AMOUNT,
    poll_seconds: float = POLL_SECONDS,
    rounds: int | None = None,
) -> int:
    """Drip until stopped (``rounds`` caps the loop) and return the count sent.

    HTTP failures are reported and retried: the node restarts, syncs and answers
    slowly, and a drip that gave up would leave the chain unable to build.
    """
    sent = 0
    completed = 0
    while rounds is None or completed < rounds:
        completed += 1
        try:
            digest = drip_round(client, params=params, to=to, amount=amount)
        except (httpx.HTTPError, DripError, ValueError) as exc:
            print(f"drip: {exc}", file=sys.stderr)
        else:
            if digest is not None:
                sent += 1
                print(f"dripped {amount} units to {to or sink_address(params)} (txid {digest})")
        if rounds is None or completed < rounds:
            time.sleep(poll_seconds)
    return sent


def main() -> None:
    """Parse arguments and drip until the process is stopped."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--node",
        default="http://node:8645",
        help="Base URL of the node that receives the transfers.",
    )
    parser.add_argument(
        "--to",
        default=None,
        help="Recipient address (default: the published sink address).",
    )
    parser.add_argument(
        "--amount",
        type=int,
        default=DRIP_AMOUNT,
        help="Transfer amount in the smallest unit (default: one coin).",
    )
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=POLL_SECONDS,
        help="Seconds between checks of the node's mempool.",
    )
    arguments = parser.parse_args()
    if arguments.poll_seconds <= 0:
        parser.error("--poll-seconds must be positive")
    if arguments.amount <= 0:
        parser.error("--amount must be positive")
    if arguments.to is not None and not validate_address(arguments.to, TESTNET):
        parser.error(f"{arguments.to!r} is not a testnet address")
    print(
        f"dripping {arguments.amount} units from the {TESTNET.network_id} faucet to "
        f"{arguments.to or sink_address()} whenever the pool is empty"
    )
    with httpx.Client(base_url=arguments.node, timeout=30) as client:
        run(
            client,
            to=arguments.to,
            amount=arguments.amount,
            poll_seconds=arguments.poll_seconds,
        )


if __name__ == "__main__":
    main()
