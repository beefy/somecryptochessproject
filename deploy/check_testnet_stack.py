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
from collections.abc import Callable, Mapping, Sequence
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


PERMANENT_FAILURES = ("no engine binary",)
"""Generator complaints no further waiting can fix: the seat is missing a file."""


def fleet_phase(status: Mapping[str, object]) -> str:
    """How far seating has got: ``starting``, ``seating``, ``online`` or ``starved``."""
    phase = status.get("phase")
    return phase if isinstance(phase, str) and phase else "unknown"


def fleet_obstacles(status: Mapping[str, object], expect_seats: int) -> tuple[str, ...]:
    """Everything the report says stands in the way of ``expect_seats`` seats.

    Shared by the timeout message and the fast-fail path so both name the same
    causes: the host budget the generator measured, the seats it failed to start,
    and its own reason for seating nothing.
    """
    problems: list[str] = []
    capacity = status.get("capacity")
    allowed = capacity.get("allowed") if isinstance(capacity, Mapping) else None
    if isinstance(allowed, int) and allowed < expect_seats:
        reason = capacity.get("reason") if isinstance(capacity, Mapping) else None
        detail = f" ({reason})" if isinstance(reason, str) and reason else ""
        problems.append(
            f"the host budget allows only {allowed} of the {expect_seats} extra miner(s){detail}"
        )
    failed = status.get("failed")
    if isinstance(failed, list):
        problems.extend(f"the generator reported: {item}" for item in failed)
    reason = status.get("reason")
    if isinstance(reason, str) and reason:
        problems.append(reason)
    return tuple(problems)


def permanent_failure(status: Mapping[str, object], expect_seats: int) -> str | None:
    """The obstacle that waiting cannot clear, or ``None`` when time may still help.

    A budget below the seat count is decided before a miner is even started, and a
    seat whose engine binary is missing never gets one. Everything else -- a
    registration still in the mempool, a nonce collision, a miner that crashed and
    is repaired next cycle -- is transient, so the check keeps polling.
    """
    capacity = status.get("capacity")
    allowed = capacity.get("allowed") if isinstance(capacity, Mapping) else None
    failed = status.get("failed")
    missing_engine = isinstance(failed, list) and any(
        marker in str(item) for item in failed for marker in PERMANENT_FAILURES
    )
    starved = isinstance(allowed, int) and allowed < expect_seats
    if starved or missing_engine:
        return "; ".join(fleet_obstacles(status, expect_seats))
    return None


def describe_progress(
    path: Path,
    status: Mapping[str, object] | None,
    *,
    elapsed: float,
    expect_seats: int,
) -> str:
    """A heartbeat line: what the report says now, and how long it has said it."""
    if status is None:
        return f"{elapsed:.0f}s: no report at {path} yet"
    roles = ", ".join(fleet_roles(status)) or "none"
    problems = "; ".join(fleet_obstacles(status, expect_seats))
    detail = f"; {problems}" if problems else ""
    return (
        f"{elapsed:.0f}s: {fleet_phase(status)}, {len(fleet_seats(status))}/{expect_seats} "
        f"seat(s) online (roles: {roles}){detail}"
    )


def wait_for_fleet(
    path: Path,
    *,
    wait_seconds: float,
    expect_seats: int = 2,
    expect_roles: Sequence[str] = (),
    poll_seconds: float = 5.0,
    progress_seconds: float = 30.0,
    progress: Callable[[str], None] | None = None,
) -> Mapping[str, object]:
    """Wait until the traffic generator reports ``expect_seats`` miners running.

    The two extra miners are children of the traffic container and are seated
    during its first cycle, which waits on registration blocks: a deploy cannot
    see them in ``docker compose ps`` and they take a minute or two to appear.

    ``progress`` is called every ``progress_seconds`` with what the report says,
    because a silent fifteen-minute poll in a CI log is indistinguishable from a
    hung job -- and the usual reason the seats are late is that the chain has
    stopped producing blocks, which the report shows as seats that never come
    online.

    A timeout reports what the generator said about why -- the host budget, a
    registration that failed, a missing engine -- instead of a bare timeout, and a
    cause no amount of waiting can clear ends the wait immediately.
    """
    deadline = time.monotonic() + wait_seconds
    started = time.monotonic()
    next_progress = started + progress_seconds
    last: Mapping[str, object] | None = None
    while True:
        status = read_fleet(path)
        if status is not None:
            last = status
            seated = len(fleet_seats(status))
            missing = [role for role in expect_roles if role not in fleet_roles(status)]
            if seated >= expect_seats and not missing:
                return status
            hopeless = permanent_failure(status, expect_seats)
            if hopeless is not None:
                raise SystemExit(
                    f"{path} cannot seat {expect_seats} extra seat(s) on this host: {hopeless}"
                )
        now = time.monotonic()
        if progress is not None and now >= next_progress:
            next_progress = now + progress_seconds
            progress(
                describe_progress(path, last, elapsed=now - started, expect_seats=expect_seats)
            )
        if now >= deadline:
            break
        time.sleep(poll_seconds)
    problems = "; ".join(fleet_obstacles(last, expect_seats)) if last is not None else ""
    if last is not None and not problems and fleet_phase(last) == "seating":
        problems = (
            "the seats are still registering: a registration is a transaction, so no "
            "block has included it yet -- check the chain is advancing "
            "(`docker compose logs --tail=50 node miner-1`)"
        )
    detail = f"; {problems}" if problems else ""
    age = ""
    if last is not None:
        updated = last.get("updated")
        if isinstance(updated, (int, float)):
            age = f"; the report was last written {max(0.0, time.time() - updated):.0f}s ago"
    raise SystemExit(
        f"{path} did not report {expect_seats} extra seat(s) "
        f"(roles: {', '.join(expect_roles) or 'any'}) in {wait_seconds:g}s{detail}{age}"
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
    parser.add_argument(
        "--progress-seconds",
        type=float,
        default=30.0,
        help="how often to report what the seat report says while waiting (default: %(default)s)",
    )
    arguments = parser.parse_args()
    if arguments.wait_seconds <= 0:
        parser.error("--wait-seconds must be positive")
    if arguments.progress_seconds <= 0:
        parser.error("--progress-seconds must be positive")
    if arguments.expect_seats < 0:
        parser.error("--expect-seats cannot be negative")
    if arguments.fleet_only and arguments.fleet is None:
        parser.error("--fleet-only needs --fleet")
    roles = tuple(role.strip() for role in arguments.expect_roles.split(",") if role.strip())
    if arguments.fleet is not None and arguments.fleet_only:
        print(
            f"waiting up to {arguments.wait_seconds:g}s for {arguments.expect_seats} "
            f"extra seat(s) (roles: {', '.join(roles) or 'any'}) in {arguments.fleet}",
            flush=True,
        )
        fleet = wait_for_fleet(
            arguments.fleet,
            wait_seconds=arguments.wait_seconds,
            expect_seats=arguments.expect_seats,
            expect_roles=roles,
            progress_seconds=arguments.progress_seconds,
            progress=print,
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
            progress_seconds=arguments.progress_seconds,
            progress=print,
        )
        print(f"extra miners ready: {describe_fleet(fleet)}")


if __name__ == "__main__":
    main()
