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

from deploy.init_testnet_stack import DEFAULT_PASSPHRASE, write_stack

from sccp.config import TESTNET
from sccp.crypto.address import validate_address

COMPOSE = Path(__file__).resolve().parents[2] / "docker-compose.yml"
"""The stack file the deploy workflow uploads to the VM."""

ADDRESS = re.compile(r"sccpt1[0-9a-z]+")


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


def test_every_address_literal_in_the_compose_file_is_a_testnet_address() -> None:
    text = COMPOSE.read_text(encoding="utf-8")
    literals = ADDRESS.findall(text)
    assert literals, "the node command lost its miner address"
    for literal in literals:
        assert validate_address(literal, TESTNET), f"{literal} is not a testnet address"
