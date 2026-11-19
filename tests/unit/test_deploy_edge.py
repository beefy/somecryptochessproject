"""The mirror's stack must stay a replica, not a second authority.

`deploy/edge/` is the only node in this repository that answers the open
internet, and it is the one nobody would notice breaking: it has no miners, so
the primary's stack check never mentions it, and no relay, so a matchmaking
failure cannot point at it. These tests pin the properties that let it run
unattended — a faucet-less node that cannot mine, a GET-only proxy on the only
public listener, loopback-only ports, and a deploy that ships the image the
primary just built and resets the replica only when the primary's chain itself
was replaced.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EDGE = ROOT / "deploy" / "edge"

COMPOSE = EDGE / "docker-compose.yml"
"""The Compose project the deploy workflow uploads to the mirror VM."""

CADDYFILE = EDGE / "Caddyfile"
"""The only public listener: TLS, GET, and a 405 for everything else."""

ENV_EXAMPLE = EDGE / ".env.example"
"""The settings an operator copies to `.env`; the workflow writes the same one."""

WORKFLOW = ROOT / ".github" / "workflows" / "deploy-edge.yml"
PRIMARY_WORKFLOW = ROOT / ".github" / "workflows" / "deploy-testnet-miners.yml"


def _option_arguments(text: str, option: str) -> list[str]:
    """Every argument that follows ``option`` in a Compose command list."""
    lines = [line.strip() for line in text.splitlines()]
    arguments: list[str] = []
    for index, line in enumerate(lines[:-1]):
        if line == f"- {option}":
            arguments.append(lines[index + 1].removeprefix("- ").strip())
    return arguments


def _service_block(text: str, service: str) -> str:
    """The lines of one service, up to the next service or top-level key."""
    lines = text.splitlines()
    start = lines.index(f"  {service}:")
    block: list[str] = []
    for line in lines[start + 1 :]:
        if line.strip() and not line.startswith("    "):
            break
        block.append(line)
    return "\n".join(block)


def test_the_mirror_is_one_node_behind_one_proxy() -> None:
    """Two services, because every extra one is another process to patch.

    In particular there is no relay and no miner here: a mirror that seated
    miners would need the roster's keys, and a relay on this host would make the
    read-only claim false the moment a browser found its port.
    """
    text = COMPOSE.read_text(encoding="utf-8")
    services = text.split("\nservices:\n", 1)[1].split("\nvolumes:\n", 1)[0]
    names = re.findall(r"^  ([a-z][a-z0-9-]*):$", services, re.M)
    assert names == ["node", "tls"], names


def test_the_mirror_cannot_spend_or_mint() -> None:
    command = _service_block(COMPOSE.read_text(encoding="utf-8"), "node")

    assert "- --no-faucet" in command
    assert "- --mine" not in command
    assert "--miner-address" not in command
    assert _option_arguments(command, "--network") == ["testnet"]


def test_the_mirror_replicates_on_a_timer_rather_than_once() -> None:
    """A startup-only sync is a snapshot, not a replica.

    ``--peer-poll-seconds 0`` (the CLI default) syncs once and then goes stale;
    the mirror has to keep asking.
    """
    command = _service_block(COMPOSE.read_text(encoding="utf-8"), "node")

    peers = _option_arguments(command, "--peer")
    assert peers == ["${SCCP_PRIMARY_URL:?set SCCP_PRIMARY_URL to the primary node's public URL}"]

    poll = _option_arguments(command, "--peer-poll-seconds")
    assert poll == ['"${SCCP_PEER_POLL_SECONDS:-30}"'], poll


def test_only_the_proxy_is_public() -> None:
    """The node binds loopback; nothing may publish 8645 or a relay port."""
    text = COMPOSE.read_text(encoding="utf-8")

    assert text.count("\n    ports:") == 1
    assert '"127.0.0.1:8645:8645"' in text
    assert "network_mode: host" in _service_block(text, "tls")
    assert "8646" not in text, "the mirror does not host the matchmaker"


def test_the_image_comes_from_the_registry_the_primary_pushes_to() -> None:
    """A local build on a 1 GB host is a slow out-of-memory, not a deploy."""
    text = COMPOSE.read_text(encoding="utf-8")
    assert "image: ${SCCP_IMAGE:-sccp-testnet:local}" in text
    assert "build: ../.." in text, "the build key is the local-development path"


def test_the_proxy_terminates_tls_and_refuses_every_write() -> None:
    text = CADDYFILE.read_text(encoding="utf-8")

    assert "{$SCCP_EDGE_DOMAIN} {" in text
    assert "reverse_proxy 127.0.0.1:8645" in text
    assert "method GET\n" in text
    assert "405" in text
    # 405 has to name what is allowed, or a client cannot tell a wrong method
    # from a wrong path. It names GET only because the node's FastAPI routes
    # register GET only: advertising HEAD here would be a promise the node
    # breaks, and `curl -I` would report a 405 the proxy never sees.
    assert 'Allow "GET"' in text
    assert "GET HEAD" not in text


def test_every_required_setting_is_in_the_env_example() -> None:
    """Both files are deployed together; a missing setting fails at `up`."""
    compose = COMPOSE.read_text(encoding="utf-8")
    required = set(re.findall(r"\$\{([A-Z0-9_]+):\?", compose))
    assert required, "the stack lost its required settings"

    example = ENV_EXAMPLE.read_text(encoding="utf-8")
    documented = set(re.findall(r"^(?:#\s*)?([A-Z0-9_]+)=", example, re.M))
    assert required <= documented, required - documented


def test_the_deploy_waits_for_the_image_the_primary_built() -> None:
    """`workflow_run` is what makes the tag exist; a path trigger would race it."""
    workflow = WORKFLOW.read_text(encoding="utf-8")
    primary = PRIMARY_WORKFLOW.read_text(encoding="utf-8")

    name = re.search(r"^name: (\S+)$", primary, re.M)
    assert name is not None
    assert f'workflows: ["{name.group(1)}"]' in workflow
    assert "types: [completed]" in workflow
    assert "conclusion == 'success'" in workflow
    assert "workflow_run.head_sha" in workflow


def test_the_mirror_deploy_resets_only_a_diverged_chain() -> None:
    """The primary's redeploy starts a new chain; the mirror has to follow it.

    A project-wide volume removal is the wrong tool on this host: it takes
    `caddy_data` with it and re-issues the certificate on every reset. The
    workflow compares block 1 with the primary's and, only on a mismatch, deletes
    the node's own data and lets one sync rebuild it.
    """
    workflow = WORKFLOW.read_text(encoding="utf-8")
    commands = "\n".join(
        line for line in workflow.splitlines() if not line.lstrip().startswith("#")
    )

    assert "down -v" not in commands
    assert "down --volumes" not in commands
    assert "docker volume" not in commands, "no reset by volume name"
    assert "rm -rf /data/*" in commands
    assert "/v1/blocks/1" in commands, "divergence is detected by the first block"
    assert "up -d --no-build" in commands
    assert "caddy reload" in commands, "a changed Caddyfile needs a reload"


def test_the_mirror_deploy_is_opt_in() -> None:
    """No mirror VM in a project must not fail the primary's deploy."""
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "vars.EDGE_DEPLOY_HOST != ''" in workflow


def test_the_deploy_proves_replication_and_read_only_access() -> None:
    """A green proxy check proves neither the chain nor the 405 contract."""
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert ".genesis_hash" in workflow, "the mirror must be on the primary's chain"
    assert '-ge "$target"' in workflow, "the height has to advance, not just answer"
    assert '[ "$code" = "405" ]' in workflow
