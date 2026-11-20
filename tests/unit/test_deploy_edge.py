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


def _step(text: str, name: str) -> str:
    """The lines of one workflow step, up to the next step."""
    marker = f"- name: {name}\n"
    start = text.index(marker)
    end = text.find("\n      - name:", start)
    return text[start:] if end == -1 else text[start:end]


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


def test_a_store_the_new_build_cannot_replay_is_rebuilt_once() -> None:
    """The mirror replays its chain, so a consensus change bricks its store.

    `restore_chain` re-runs every stored block through validation under the
    running build's parameters. The 30 s cadence doubled maturity, registration,
    dispute and beacon windows, which changes the state root of blocks already
    on disk: the node aborts at startup, never turns healthy, and `up` fails
    behind `tls`'s `service_healthy` dependency -- before the block-1 comparison
    above can notice. `up -d` returning success for a restart loop is exactly
    the trap, so the step waits for health and, when it fails, prints why and
    throws the cache away once.
    """
    restart = _step(WORKFLOW.read_text(encoding="utf-8"), "Pull the image and restart the stack")

    assert "--wait --wait-timeout" in restart, "a created container is not a serving node"
    assert restart.index("--wait --wait-timeout") < restart.index("rm -rf /data/*")
    assert restart.count("--wait --wait-timeout 300 node") == 2, "both waits are the node's"
    assert restart.count("rm -rf /data/*") == 1, "one rebuild, not a wipe loop"
    assert "docker compose logs --tail=40 --no-color node" in restart, "say why it was wiped"
    # The proxy and the rest of the project still start, and only after the node
    # is healthy: `tls` gates on `service_healthy`, so starting it first waits on
    # a node that is still being rebuilt.
    whole = 'docker compose up -d --no-build --remove-orphans"'
    assert restart.count(whole) == 1
    assert restart.index(whole) > restart.index("rm -rf /data/*")


def test_a_missing_mirror_is_reported_rather_than_skipped() -> None:
    """A job stopped by `if` is invisible: no steps, no log, no reason.

    This workflow first shipped with `vars.EDGE_DEPLOY_HOST != ''` in the
    job-level `if`, and its first real run showed up as "skipped" with an empty
    step list — indistinguishable from a workflow that never worked. The decision
    belongs in a step, which also means variables scoped to the `testnet`
    environment are visible (job-level contexts resolve before `environment:`).
    """
    workflow = WORKFLOW.read_text(encoding="utf-8")
    job = workflow.split("\njobs:\n", 1)[1].split("\n    steps:\n", 1)[0]

    assert "vars." not in job, "configuration must not gate the job itself"
    assert "steps.target.outputs.deploy == 'true'" in workflow
    assert "deploy=false" in workflow
    assert "::notice" in workflow, "the run has to say why nothing happened"
    assert "GITHUB_STEP_SUMMARY" in workflow


def test_the_mirror_host_key_is_verified_even_when_one_is_pinned() -> None:
    """The pinned file covers the primary, so it cannot cover the mirror.

    `EDGE_KNOWN_HOSTS` falls back to `TESTNET_KNOWN_HOSTS`, which pins the host
    the fallback was written for. Using it as-is would make
    `StrictHostKeyChecking yes` refuse the mirror — the one host this job exists
    to reach — so the mirror's key is scanned outside the pinned-file branch.
    A deliberate `EDGE_KNOWN_HOSTS` that already holds the mirror is the
    exception: that probe is skipped, because a run holding the key does not
    need a route to read it again.
    """
    step = _step(WORKFLOW.read_text(encoding="utf-8"), "Configure SSH")

    write_pin = step.index('if [ -n "${KNOWN_HOSTS:-}" ]; then')
    end_of_pin = step.index("fi\n", write_pin)
    assert '"$edge_host"' not in step[write_pin:end_of_pin], "the scan is not nested in the pin"
    assert "ssh primary \"ssh-keyscan -p $DEPLOY_PORT -H '$edge_host'\"" in step
    assert 'ssh-keyscan -T 10 -p "$DEPLOY_PORT" -H "$edge_host"' in step
    assert 'if [ -n "${EDGE_PIN:-}" ] && pinned "$edge_host" "$DEPLOY_PORT"; then' in step


def test_the_jump_is_tried_first_and_the_direct_scan_is_the_fallback() -> None:
    """Both routes are attempted, so neither needs a variable to exist.

    The step first demanded `vars.EDGE_SSH_PROXY_JUMP=1` before it would hop
    through the primary. On the first run with a real mirror -- a public address
    whose only inbound ports are 80/443 -- the job scanned that address, found no
    key, and told the operator to set a variable that was never part of the
    setup. The documented posture is a jump, because the mirror has no public SSH
    at all, so the jump is tried first and the direct scan only runs when it
    finds nothing: `1` and `0` force a route, unset tries both.
    """
    step = _step(WORKFLOW.read_text(encoding="utf-8"), "Configure SSH")

    assert 'case "${JUMP:-}" in' in step
    assert "1 | true | yes | on) mode=jump ;;" in step
    assert "0 | false | no | off) mode=direct ;;" in step
    assert "mode=any" in step
    assert "vars.EDGE_SSH_PROXY_JUMP must be 1 or 0" in step

    jump_probe = step.index('ssh primary "ssh-keyscan')
    direct_probe = step.index('ssh-keyscan -T 10 -p "$DEPLOY_PORT" -H "$edge_host"')
    assert jump_probe < direct_probe, "the route that needs no public port is tried first"
    assert 'if [ -z "$route" ] && [ "$mirror_pinned" = false ] && [ "$mode" != jump ]; then' in step

    # The alias carries the route that answered, not the raw flag: with the flag
    # unset a jump host would otherwise produce a Host block that never used it.
    assert (
        'host_block edge "$TARGET" "$DEPLOY_PORT" "$HOME/.ssh/id_edge" "ProxyJump primary"'
    ) in step
    assert "${JUMP:+ProxyJump primary}" not in step, "the alias must follow the route"
    # Whichever route won is in the log, so a green run says how it reached the
    # mirror and a red one does not have to be guessed at.
    assert "jumped through ${PRIMARY_TARGET#*@}" in step
    assert "direct from this runner" in step


def test_the_jump_hop_uses_the_primary_s_key_not_the_mirror_s() -> None:
    """Two hosts, two keys: the primary trusts the deploy key, not the mirror's.

    One `IdentityFile` for both `Host` blocks made the hop impossible whenever
    the keys differed -- which is what this repository ships, because
    `EDGE_SSH_KEY` is the mirror's own key and `TESTNET_SSH_KEY` is the primary's.
    The jump hop now has its own identity file, so the hop authenticates to the
    primary with the key the primary already trusts.
    """
    step = _step(WORKFLOW.read_text(encoding="utf-8"), "Configure SSH")

    assert "PRIMARY_KEY: ${{ secrets.TESTNET_SSH_KEY }}" in step
    assert '> "$HOME/.ssh/id_primary"' in step
    assert 'primary_key="$HOME/.ssh/id_primary"' in step
    assert '"$primary_key" ""' in step, "the primary block takes the primary identity"
    assert '"$HOME/.ssh/id_edge" "ProxyJump primary"' in step


def test_an_unauthorized_deploy_key_is_named_before_the_scp() -> None:
    """A host key proves identity, not authorization; say which one failed.

    `EDGE_KNOWN_HOSTS` can be right and the key still absent from the mirror's
    `ssh-keys` metadata, which used to surface as a bare `scp` failure two steps
    after the step that could have known. The one deliberate connect also tells a
    refused key apart from a route that never reached the host, because those two
    need different fixes.
    """
    step = _step(WORKFLOW.read_text(encoding="utf-8"), "Configure SSH")

    assert "ssh -o ConnectTimeout=15 edge true" in step
    assert "'Permission denied' means the key is not in the mirror's ssh-keys metadata" in step
    assert "accepts TCP on port $DEPLOY_PORT but never answers is unwell" in step


def test_no_host_key_probe_can_fail_without_saying_why() -> None:
    """This step's first real run was five seconds and "exit code 1", nothing else.

    `ssh-keyscan` gives up after five seconds and exits 0 having printed nothing
    when a responder answers without a key, so a probe is never trusted: each one
    is followed by a `pinned` check that decides the outcome, and every terminal
    outcome ends in a `fail` naming the setting or the host to look at. The step
    grew a third probe (jump host, mirror through the jump, mirror directly), and
    the fallback between them is why a probe no longer has to fail the job
    itself: no key from one route is an invitation to try the other.
    """
    step = _step(WORKFLOW.read_text(encoding="utf-8"), "Configure SSH")
    lines = step.splitlines()

    probes = [
        index
        for index, line in enumerate(lines)
        if not line.strip().startswith("#")
        and ("ssh-keyscan" in line or line.strip().startswith("ssh primary"))
    ]
    assert len(probes) == 3, "one jump-host scan and the two mirror scans"
    for index in probes:
        window = "\n".join(lines[index : index + 4])
        assert "pinned" in window, f"the probe at line {index + 1} decides without verifying"

    for expected in (
        "neither EDGE_SSH_KEY nor TESTNET_SSH_KEY is configured",
        "vars.EDGE_DEPLOY_HOST must be user@host",
        "vars.EDGE_SSH_PROXY_JUMP is set but vars.TESTNET_DEPLOY_HOST is empty",
        "the jump host",
        "must be the mirror's internal address",
        "sccp-edge-ssh-from-primary",
        "pin its key in secrets.EDGE_KNOWN_HOSTS",
        "'Permission denied' means the key is not in the mirror's ssh-keys metadata",
    ):
        assert expected in step, expected


def test_the_ssh_step_cannot_prompt_or_sit_on_a_dead_connection() -> None:
    """An unattended runner has to fail, not wait for a keyboard or a timeout.

    `BatchMode` turns a passphrase prompt into an authentication failure, and a
    `ConnectTimeout` bounds a dropped SYN that would otherwise hold the job open
    until GitHub's own limit. `ssh-keyscan` is given an explicit `-T` rather than
    its five-second default, which is the number that used to look arbitrary.
    """
    step = _step(WORKFLOW.read_text(encoding="utf-8"), "Configure SSH")

    assert "BatchMode yes" in step
    assert "ConnectTimeout 15" in step
    assert "ssh-keyscan -T 10" in step


def test_the_deploy_proves_replication_and_read_only_access() -> None:
    """A green proxy check proves neither the chain nor the 405 contract."""
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert ".genesis_hash" in workflow, "the mirror must be on the primary's chain"
    assert '-ge "$target"' in workflow, "the height has to advance, not just answer"
    assert '[ "$code" = "405" ]' in workflow
