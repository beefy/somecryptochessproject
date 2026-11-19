"""The deploy-time smoke checker, including the report the extra seats publish.

The two extra miners are children of the traffic container, so the checker reads
``/stack/synthetic-fleet.json`` to prove they came up. These tests cover that
reader, and the ``--fleet-only`` mode the deploy workflow runs inside the
traffic container.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest
from deploy import check_testnet_stack as checker

Seats = Sequence[tuple[str, str]]


def write_report(
    path: Path,
    seats: Seats,
    *,
    failed: Sequence[str] = (),
    allowed: int = 2,
    reason: str = "a test budget",
) -> Path:
    """Write the file ``synthetic_miner_creation`` publishes, in miniature."""
    payload = {
        "capacity": {"allowed": allowed, "reason": reason},
        "online_limit": allowed,
        "online": [
            {"name": name, "index": index, "role": role, "address": f"sccpt1seat{index}"}
            for index, (name, role) in enumerate(seats)
        ],
        "failed": list(failed),
        "reason": "" if seats else reason,
        "updated": 1_700_000_000.0,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


BOTH: Seats = (("synthetic-miner-1", "slow"), ("synthetic-miner-2", "gnuchess"))


def test_both_seats_satisfy_the_check(tmp_path: Path) -> None:
    path = write_report(tmp_path / "synthetic-fleet.json", BOTH)

    status = checker.wait_for_fleet(
        path, wait_seconds=0, expect_seats=2, expect_roles=("slow", "gnuchess")
    )

    assert len(checker.fleet_seats(status)) == 2
    assert checker.fleet_roles(status) == ("slow", "gnuchess")
    line = checker.describe_fleet(status)
    assert "synthetic-miner-1" in line
    assert "budget allows 2" in line


def test_a_report_that_lags_the_deploy_is_polled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The seats register during the first cycle, so the file can appear late."""
    path = tmp_path / "synthetic-fleet.json"
    wrote: list[float] = []

    def sleep(seconds: float) -> None:
        wrote.append(seconds)
        write_report(path, BOTH)

    monkeypatch.setattr(checker.time, "sleep", sleep)

    status = checker.wait_for_fleet(path, wait_seconds=30, expect_seats=2, poll_seconds=0.5)

    assert wrote == [0.5]
    assert checker.fleet_roles(status) == ("slow", "gnuchess")


def test_one_seat_is_not_enough(tmp_path: Path) -> None:
    path = write_report(tmp_path / "synthetic-fleet.json", BOTH[:1])

    with pytest.raises(SystemExit):
        checker.wait_for_fleet(path, wait_seconds=0, expect_seats=2)


def test_missing_role_fails_even_with_enough_seats(tmp_path: Path) -> None:
    """Two honest seats are not the deployment: the roles are the point."""
    seats = (("synthetic-miner-1", "honest"), ("synthetic-miner-2", "honest"))
    path = write_report(tmp_path / "synthetic-fleet.json", seats)

    with pytest.raises(SystemExit, match="slow"):
        checker.wait_for_fleet(path, wait_seconds=0, expect_seats=2, expect_roles=("slow",))


def test_a_timeout_says_why_nothing_was_seated(tmp_path: Path) -> None:
    """The failure has to name the cause: budget, engine or registration."""
    path = write_report(
        tmp_path / "synthetic-fleet.json",
        (),
        failed=["synthetic-miner-2: no engine binary at /usr/games/gnuchess"],
        allowed=1,
        reason="the host has 2 CPU(s) and 1200 MiB free",
    )

    with pytest.raises(SystemExit) as failure:
        checker.wait_for_fleet(path, wait_seconds=0, expect_seats=2)

    message = str(failure.value)
    assert "no engine binary" in message
    assert "allows only 1" in message


def test_an_unreadable_report_is_not_a_crash(tmp_path: Path) -> None:
    path = tmp_path / "synthetic-fleet.json"
    path.write_text("{not json", encoding="utf-8")

    assert checker.read_fleet(path) is None
    with pytest.raises(SystemExit):
        checker.wait_for_fleet(path, wait_seconds=0, expect_seats=1)


def test_fleet_only_checks_the_seats_and_opens_no_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = write_report(tmp_path / "synthetic-fleet.json", BOTH)

    def unreachable(*args: object, **kwargs: object) -> None:
        raise AssertionError("the fleet-only check touched the node or the relay")

    monkeypatch.setattr(checker, "wait_for_node", unreachable)
    monkeypatch.setattr(checker, "wait_for_relay", unreachable)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "check_testnet_stack.py",
            "--fleet",
            str(path),
            "--fleet-only",
            "--expect-roles",
            "slow, gnuchess",
        ],
    )

    checker.main()

    assert "extra miners ready: 2 extra seat(s) online" in capsys.readouterr().out


def test_fleet_only_needs_a_report_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["check_testnet_stack.py", "--fleet-only"])

    with pytest.raises(SystemExit) as failure:
        checker.main()

    assert failure.value.code == 2
