"""The deployment's compose file must spell addresses the code still accepts.

`docker compose up` with a stale address literal is not a cosmetic bug: the node
validates `--miner-address` before it binds the port, exits with status 2, and
every service that waits for the node is skipped, so the stack comes up empty and
the redeploy workflow fails on "container ... is unhealthy".

That is exactly what happened when the one-scheme refactor moved addresses to
version 1 — a 33-byte program committing to the 1985-byte hybrid public key.
`docker-compose.yml` kept a version-0 literal from the Ed25519 era, which
`decode_address` now rejects as "unsupported address version 0".
"""

from __future__ import annotations

import re
from pathlib import Path

import httpx
import pytest
from deploy import seed_testnet_demo as seed
from deploy import synthetic_testnet_traffic as traffic
from deploy.init_testnet_stack import DEFAULT_PASSPHRASE, write_stack

from sccp.config import TESTNET
from sccp.crypto.address import validate_address

COMPOSE = Path(__file__).resolve().parents[2] / "docker-compose.yml"
"""The stack file the deploy workflow uploads to the VM."""

ADDRESS = re.compile(r"sccpt1[0-9a-z]+")

TRAFFIC = "/app/deploy/synthetic_testnet_traffic.py"
"""The generator's script inside the container, as the traffic service runs it."""

SEED = "deploy.seed_testnet_demo"
"""The demo pool and bridge seeder module, run with ``python -m``."""


def _option_arguments(text: str, option: str) -> list[str]:
    """Every argument that follows ``option`` in a compose command list."""
    lines = [line.strip() for line in text.splitlines()]
    arguments: list[str] = []
    for index, line in enumerate(lines[:-1]):
        if line == f"- {option}":
            arguments.append(lines[index + 1].removeprefix("- ").strip())
    return arguments


def test_the_node_mines_to_the_address_the_stack_writes(tmp_path: Path) -> None:
    text = COMPOSE.read_text(encoding="utf-8")
    passphrase = re.search(r"SCCP_STACK_PASSPHRASE:\s*(\S+)", text)
    assert passphrase is not None, "the init service no longer pins the stack passphrase"
    assert passphrase.group(1) == DEFAULT_PASSPHRASE

    arguments = _option_arguments(text, "--miner-address")
    assert len(arguments) == 1, "expected exactly one --miner-address (the node's miner)"

    summary = write_stack(tmp_path, passphrase=passphrase.group(1))
    miners = summary["miners"]
    assert isinstance(miners, list)
    expected = str(miners[0]["address"])

    assert arguments[0] == expected
    assert validate_address(arguments[0], TESTNET)


def test_the_stack_runs_two_miners_per_variant() -> None:
    """Four fixed miners: one standard pair and one Chess960 pair."""
    text = COMPOSE.read_text(encoding="utf-8")
    for index in range(1, 5):
        assert f"miner-{index}:" in text, f"miner-{index} service is missing"
    variants = sorted(_option_arguments(text, "--variant"))
    assert variants == ["chess960", "chess960", "standard", "standard"]


def test_every_address_literal_in_the_compose_file_is_a_testnet_address() -> None:
    text = COMPOSE.read_text(encoding="utf-8")
    literals = ADDRESS.findall(text)
    assert literals, "the node command lost its miner address"
    for literal in literals:
        assert validate_address(literal, TESTNET), f"{literal} is not a testnet address"


def _service_arguments(text: str, entrypoint: str) -> list[str]:
    """The arguments the service that runs ``entrypoint`` passes to it."""
    lines = text.splitlines()
    markers = (
        f'entrypoint: ["python", "{entrypoint}"]',
        f'entrypoint: ["python", "-m", "{entrypoint}"]',
    )
    start = next(
        (index for index, line in enumerate(lines) if any(marker in line for marker in markers)),
        None,
    )
    assert start is not None, f"no service runs {entrypoint}"
    arguments: list[str] = []
    in_command = False
    for line in lines[start + 1 :]:
        stripped = line.strip()
        if stripped == "command:":
            in_command = True
            continue
        if not in_command or stripped.startswith("#"):
            continue
        if not stripped.startswith("- "):
            break
        arguments.append(stripped.removeprefix("- ").strip().strip('"'))
    return arguments


def test_the_seed_command_speaks_the_flags_the_seeder_accepts() -> None:
    """The seed service must not exit before the bridge or the pool exists."""
    argv = _service_arguments(COMPOSE.read_text(encoding="utf-8"), SEED)
    assert argv == ["--stack", "/stack", "--node", "http://node:8645"]
    parsed = seed._parser().parse_args(argv)
    assert (parsed.stack.name, parsed.node) == ("stack", "http://node:8645")


def test_the_traffic_command_speaks_the_flags_the_generator_accepts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale flag would make the traffic service exit before it sends anything."""
    argv = _service_arguments(COMPOSE.read_text(encoding="utf-8"), TRAFFIC)
    assert argv, "the traffic service lost its command"
    captured: dict[str, object] = {}

    def fake_run(client: httpx.Client, **kwargs: object) -> list[traffic.CycleReport]:
        captured.update(kwargs)
        return []

    monkeypatch.setattr(traffic, "run", fake_run)
    assert traffic.main(argv) == 0

    options = captured["options"]
    assert isinstance(options, traffic.Options)
    assert (options.node, options.relay) == ("http://node:8645", "http://relay:8646")
    # The two extra roles are seated, kept up, and given the witness keys a claim
    # needs: the slow seat produces flags and the alternate engine diversifies the
    # roster, so neither is a churn demo here.
    assert options.extra_miners == 2
    assert options.max_extra_online == 2
    assert options.extra_miner_churn is False
    assert options.witness_key_files == traffic.EXTRA_WITNESS_KEY_FILES
    assert options.witness_urls == ("http://witness-2:8647", "http://witness-3:8648")


def test_the_stack_runs_three_clock_witnesses() -> None:
    """The relay signs, and two independent services audit and co-sign.

    Without the relay's ``--witness-key`` no receipt is signed, so a flagged game
    cannot become a bond-backed timeout claim: the whole timeout path is dead on
    the deployment. The two extra witnesses are what make the role redundant.
    """
    text = COMPOSE.read_text(encoding="utf-8")
    assert "- --witness-key" in text
    assert "/stack/keys/witness-1.key" in text
    for name in ("witness-2:", "witness-3:"):
        assert name in text, f"{name} service is missing"
    for index in (1, 2, 3):
        # Four miners, two witness services and the extra miner fleet each read
        # the public key file.
        assert text.count(f"/stack/keys/witness-{index}.pub") == 7
    assert text.count("- --relay-witness-key-file") == 18
    assert (
        _option_arguments(text, "--witness-url")
        == [
            "http://witness-2:8647",
            "http://witness-3:8648",
        ]
        * 5
    )
    assert "8647:8647" not in text and "8648:8648" not in text, (
        "the witnesses are internal evidence sources, not a published API"
    )
