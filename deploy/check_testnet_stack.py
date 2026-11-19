"""Smoke-check a running testnet stack.

The deploy workflow calls this inside the node container after ``docker compose
up``: it waits for the bootstrap height, then for the relay to expose the epoch
the bootstrap wrote, and finally that the relay is attesting (it has a clock
witness key). A second invocation, inside the traffic container, checks the two
extra seats with ``--fleet`` and ``--fleet-only``: those miners are children of
that container, so nothing else can see them. It is also useful locally::

    python deploy/check_testnet_stack.py --node http://127.0.0.1:8645 \
        --relay http://127.0.0.1:8646

Exit status is zero only when both services answer, the relay has at least one
match, and the relay signs its receipts. It does not wait for a game to finish
(that depends on engine speed).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path

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


def read_fleet(path: Path) -> Mapping[str, object] | None:
    """The fleet's status file, or ``None`` when it is missing or unreadable.

    ``synthetic_testnet_traffic`` rewrites the file at the start of every cycle,
    so a missing or half-written file is an ordinary state to poll through.
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, Mapping) else None


def fleet_seats(status: Mapping[str, object]) -> tuple[dict[str, object], ...]:
    """The seats ``status`` reports as running, with the fields a check needs."""
    online = status.get("online")
    if not isinstance(online, list):
        return ()
    return tuple(seat for seat in online if isinstance(seat, dict))


def fleet_roles(status: Mapping[str, object]) -> tuple[str, ...]:
    """The line-up labels of the running seats (``slow``, ``gnuchess``, ...)."""
    roles: list[str] = []
    for seat in fleet_seats(status):
        role = seat.get("role")
        if isinstance(role, str) and role:
            roles.append(role)
    return tuple(roles)


def describe_fleet(status: Mapping[str, object]) -> str:
    """One line naming the seats and the host budget underneath them."""
    seats = fleet_seats(status)
    names = ", ".join(str(seat.get("name", "?")) for seat in seats) or "none"
    roles = ", ".join(fleet_roles(status)) or "none"
    capacity = status.get("capacity")
    budget = ""
    if isinstance(capacity, Mapping) and isinstance(capacity.get("allowed"), int):
        budget = f", budget allows {capacity['allowed']}"
    return f"{len(seats)} extra seat(s) online: {names} (roles: {roles}{budget})"


def wait_for_fleet(
    path: Path,
    *,
    wait_seconds: float,
    expect_seats: int = 2,
    expect_roles: Sequence[str] = (),
    poll_seconds: float = 5.0,
) -> Mapping[str, object]:
    """Wait until the traffic generator reports ``expect_seats`` miners running.

    The two extra miners are children of the traffic container and are seated
    during its first cycle, which waits on registration blocks: a deploy cannot
    see them in ``docker compose ps`` and they take a minute or two to appear.
    A timeout reports what the generator said about why -- the host budget, a
    registration that failed, a missing engine -- instead of a bare timeout.
    """
    deadline = time.monotonic() + wait_seconds
    last: Mapping[str, object] | None = None
    while True:
        status = read_fleet(path)
        if status is not None:
            last = status
            seated = len(fleet_seats(status))
            missing = [role for role in expect_roles if role not in fleet_roles(status)]
            if seated >= expect_seats and not missing:
                return status
        if time.monotonic() >= deadline:
            break
        time.sleep(poll_seconds)
    detail = ""
    if last is not None:
        failed = last.get("failed")
        if isinstance(failed, list) and failed:
            detail += "; the generator reported: " + "; ".join(str(item) for item in failed)
        reason = last.get("reason")
        if isinstance(reason, str) and reason:
            detail += f"; {reason}"
        capacity = last.get("capacity")
        if isinstance(capacity, Mapping):
            asked = capacity.get("allowed")
            if isinstance(asked, int) and asked < expect_seats:
                detail += f"; the host budget allows only {asked} extra miner(s)"
    raise SystemExit(
        f"{path} did not report {expect_seats} extra seat(s) "
        f"(roles: {', '.join(expect_roles) or 'any'}) in {wait_seconds:g}s{detail}"
    )


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
    parser.add_argument(
        "--fleet",
        type=Path,
        default=None,
        help="the traffic generator's seat report, written to /stack by default",
    )
    parser.add_argument(
        "--expect-seats",
        type=int,
        default=2,
        help="extra miners that must be running (default: %(default)s)",
    )
    parser.add_argument(
        "--expect-roles",
        default="",
        help="comma-separated line-up labels that must be seated, e.g. slow,gnuchess",
    )
    parser.add_argument(
        "--fleet-only",
        action="store_true",
        help="check only the extra seats (for use inside the traffic container)",
    )
    arguments = parser.parse_args()
    if arguments.wait_seconds <= 0:
        parser.error("--wait-seconds must be positive")
    if arguments.expect_seats < 0:
        parser.error("--expect-seats cannot be negative")
    if arguments.fleet_only and arguments.fleet is None:
        parser.error("--fleet-only needs --fleet")
    roles = tuple(role.strip() for role in arguments.expect_roles.split(",") if role.strip())
    if arguments.fleet is not None and arguments.fleet_only:
        fleet = wait_for_fleet(
            arguments.fleet,
            wait_seconds=arguments.wait_seconds,
            expect_seats=arguments.expect_seats,
            expect_roles=roles,
        )
        print(f"extra miners ready: {describe_fleet(fleet)}")
        return
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
    if arguments.fleet is not None:
        fleet = wait_for_fleet(
            arguments.fleet,
            wait_seconds=arguments.wait_seconds,
            expect_seats=arguments.expect_seats,
            expect_roles=roles,
        )
        print(f"extra miners ready: {describe_fleet(fleet)}")


if __name__ == "__main__":
    main()
