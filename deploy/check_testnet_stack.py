"""Smoke-check a running testnet stack.

The deploy workflow calls this inside the node container after ``docker compose
up``: it waits for the bootstrap height, then for the relay to expose the epoch
the bootstrap wrote, and finally that the relay is attesting (it has a clock
witness key). It is also useful locally::

    python deploy/check_testnet_stack.py --node http://127.0.0.1:8645 \
        --relay http://127.0.0.1:8646

Exit status is zero only when both services answer, the relay has at least one
match, and the relay signs its receipts. It does not wait for a game to finish
(that depends on engine speed).
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Mapping

import httpx

from sccp.chain.state import epoch_start_height
from sccp.config import TESTNET

BOOTSTRAP_HEIGHT = epoch_start_height(4, TESTNET) - 1
"""Last block of epoch 3, matching ``bootstrap_testnet_stack.BOOTSTRAP_HEIGHT``."""


def _get_object(client: httpx.Client, path: str) -> Mapping[str, object] | None:
    try:
        response = client.get(path)
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError):
        return None
    return payload if isinstance(payload, Mapping) else None


def wait_for_node(client: httpx.Client, *, wait_seconds: float) -> Mapping[str, object]:
    """Wait until the node is at least at the bootstrap height."""
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        status = _get_object(client, "/v1/status")
        if status is not None:
            height = status.get("height")
            if (
                isinstance(height, int)
                and not isinstance(height, bool)
                and height >= BOOTSTRAP_HEIGHT
            ):
                return status
        time.sleep(2)
    raise SystemExit(f"node did not reach height {BOOTSTRAP_HEIGHT} in {wait_seconds:g}s")


def wait_for_relay(client: httpx.Client, *, wait_seconds: float) -> Mapping[str, object]:
    """Wait until the relay reports at least one match."""
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        status = _get_object(client, "/v1/status")
        if status is not None:
            matches = status.get("matches")
            if isinstance(matches, int) and not isinstance(matches, bool) and matches >= 1:
                return status
        time.sleep(2)
    raise SystemExit(f"relay did not become ready in {wait_seconds:g}s")


def check_witness(relay_status: Mapping[str, object], *, required: bool = True) -> str | None:
    """The relay's witness address, or a failure when the stack should have one.

    A relay without ``--witness-key`` publishes unsigned receipts, so a flagged
    game cannot become a bonded ``timeout_claim``: the timeout half of the
    protocol is dead on that deployment. That is worth failing a deploy over.
    """
    witness = relay_status.get("witness")
    if isinstance(witness, str) and witness:
        return witness
    if required:
        raise SystemExit(
            "the relay has no clock witness key: receipts are unsigned and no "
            "timeout claim can settle (pass --allow-unattested to deploy anyway)"
        )
    return None


def main() -> None:
    """Parse arguments and run the readiness checks."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node", default="http://127.0.0.1:8645")
    parser.add_argument("--relay", default="http://127.0.0.1:8646")
    parser.add_argument("--wait-seconds", type=float, default=180.0)
    parser.add_argument(
        "--allow-unattested",
        action="store_true",
        help="accept a relay without a clock witness key (an unattested testnet)",
    )
    arguments = parser.parse_args()
    if arguments.wait_seconds <= 0:
        parser.error("--wait-seconds must be positive")
    with httpx.Client(base_url=arguments.node, timeout=10) as node:
        status = wait_for_node(node, wait_seconds=arguments.wait_seconds)
    with httpx.Client(base_url=arguments.relay, timeout=10) as relay:
        relay_status = wait_for_relay(relay, wait_seconds=30)
    matches = relay_status.get("matches")
    if not isinstance(matches, int) or matches < 1:
        print(f"relay has no matches: {dict(relay_status)}", file=sys.stderr)
        raise SystemExit(1)
    witness = check_witness(relay_status, required=not arguments.allow_unattested)
    attested = f", witness {witness}" if witness else ", unattested"
    print(
        f"testnet ready: node height {status['height']}, "
        f"relay epoch {relay_status.get('epoch')} with {matches} match(es){attested}"
    )


if __name__ == "__main__":
    main()
