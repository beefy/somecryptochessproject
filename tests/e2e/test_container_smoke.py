"""Build the image and exercise a live mining node with real side traffic.

The rest of the end-to-end suite runs the Python packages directly. This test
starts from the artefact a deployment actually ships: it builds the current
working tree with ``docker build``, creates a private network, and runs the
node, the traffic generator and the drip as separate containers. The node mines
on its own, the drip keeps a quiet chain supplied with ledger work, and the
traffic generator funds wallets, sends transfers, and locks, claims and refunds
HTLCs.

It is opt-in because it needs a Docker daemon and a few minutes of wall clock.
Set ``SCCP_RUN_CONTAINER_E2E=1`` and select the ``container`` marker::

    SCCP_RUN_CONTAINER_E2E=1 pytest -m container tests/e2e/test_container_smoke.py -s

``make container-e2e`` does the same. Set ``SCCP_CONTAINER_E2E_IMAGE`` to an
already-built tag to skip the build while iterating on the test itself.
"""

from __future__ import annotations

import os
import socket
import subprocess
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from sccp.chain.block import bootstrap_end_height
from sccp.config import TESTNET, coins_to_units
from sccp.crypto.address import encode_address, validate_address
from sccp.crypto.pq import hybrid_keypair_from_seed

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

NODE_CONTAINER_PORT = 8645
NODE_ALIAS = "node"

MINER_SEED = b"\x5c" * 32
MINER_ADDRESS = encode_address(hybrid_keypair_from_seed(MINER_SEED).public_key_bytes, TESTNET)

BOOTSTRAP_TIMEOUT_SECONDS = 120.0
REQUEST_TIMEOUT_SECONDS = 10.0
STARTUP_TIMEOUT_SECONDS = 60.0
TRAFFIC_TIMEOUT_SECONDS = 600.0
BUILD_TIMEOUT_SECONDS = 1_800.0

# The image is built from the current tree rather than a compose tag: the
# published `sccp-testnet:local` image can predate the HTLC and DEX routes this
# test asserts. The override exists only for fast local iteration.
IMAGE_OVERRIDE_ENV = "SCCP_CONTAINER_E2E_IMAGE"
GATE_ENV = "SCCP_RUN_CONTAINER_E2E"

pytestmark = [
    pytest.mark.container,
    pytest.mark.skipif(
        os.environ.get(GATE_ENV) != "1",
        reason=f"set {GATE_ENV}=1 to build and run the Docker image",
    ),
]


def _free_port() -> int:
    """Reserve a loopback port for the node's published API."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _docker(
    *arguments: str,
    check: bool = True,
    timeout: float = 120.0,
) -> subprocess.CompletedProcess[str]:
    """Run ``docker`` and fail the test with its output when it does not succeed."""
    command = ["docker", *arguments]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            check=False,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        pytest.skip("docker is not installed")
    except subprocess.TimeoutExpired:
        pytest.fail(f"{' '.join(command)} timed out after {timeout:.0f}s")
    if check and completed.returncode != 0:
        pytest.fail(
            f"{' '.join(command)} failed with exit code {completed.returncode}\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
    return completed


def _docker_logs(name: str, *, tail: int = 120) -> str:
    """The tail of a container's output, for a failure report."""
    completed = _docker("logs", "--tail", str(tail), name, check=False)
    return f"{completed.stdout}{completed.stderr}"


def _wait_for_json(
    url: str,
    predicate: Callable[[dict[str, Any]], bool],
    *,
    description: str,
    timeout: float,
    interval: float = 0.2,
) -> dict[str, Any]:
    """Poll ``url`` until its JSON document satisfies ``predicate``."""
    deadline = time.monotonic() + timeout
    last = "no response"
    with httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS) as client:
        while time.monotonic() < deadline:
            try:
                document = client.get(url).json()
            except (httpx.HTTPError, ValueError) as exc:
                last = str(exc)
            else:
                if isinstance(document, dict) and predicate(document):
                    return document
                last = repr(document)[:500]
            time.sleep(interval)
    pytest.fail(f"timed out waiting for {description}; last response: {last}")


def _wait_for_log(name: str, needle: str, *, timeout: float) -> None:
    """Wait until a container has logged ``needle``."""
    deadline = time.monotonic() + timeout
    observed = ""
    while time.monotonic() < deadline:
        observed = _docker_logs(name, tail=200)
        if needle in observed:
            return
        time.sleep(0.2)
    pytest.fail(f"timed out waiting for {needle!r} in {name} logs:\n{observed}")


@pytest.fixture(scope="session")
def node_image() -> Iterator[str]:
    """Build the current tree, or reuse a tag named by the override variable."""
    override = os.environ.get(IMAGE_OVERRIDE_ENV)
    if override:
        yield override
        return
    tag = f"sccp-testnet:container-e2e-{uuid.uuid4().hex[:12]}"
    _docker(
        "build",
        "--quiet",
        "--tag",
        tag,
        str(REPOSITORY_ROOT),
        timeout=BUILD_TIMEOUT_SECONDS,
    )
    try:
        yield tag
    finally:
        _docker("image", "rm", "--force", tag, check=False)


@pytest.fixture(scope="session")
def private_network() -> Iterator[str]:
    """A per-run bridge network so container names cannot collide with a stack."""
    name = f"sccp-container-e2e-{uuid.uuid4().hex[:10]}"
    _docker("network", "create", name)
    try:
        yield name
    finally:
        _docker("network", "rm", name, check=False)


@pytest.fixture(scope="session")
def node(node_image: str, private_network: str) -> Iterator[str]:
    """A testnet node that mines on its own and publishes its API on loopback."""
    name = f"{private_network}-node"
    host_port = _free_port()
    published = f"127.0.0.1:{host_port}:{NODE_CONTAINER_PORT}"
    _docker(
        "run",
        "--detach",
        "--rm",
        "--name",
        name,
        "--network",
        private_network,
        "--network-alias",
        NODE_ALIAS,
        # An anonymous volume inherits the image's `sccp` ownership. A tmpfs
        # mount would be root-owned and the unprivileged node user could not
        # create SQLite files.
        "--volume",
        "/data",
        "--publish",
        published,
        node_image,
        "--data-dir",
        "/data",
        "node",
        "run",
        "--network",
        "testnet",
        "--host",
        "0.0.0.0",
        "--port",
        str(NODE_CONTAINER_PORT),
        "--mine",
        "--miner-address",
        MINER_ADDRESS,
        "--mine-poll-seconds",
        "0.2",
        "--mine-interval-seconds",
        "1",
    )
    url = f"http://127.0.0.1:{host_port}"
    try:
        _wait_for_json(
            f"{url}/v1/status",
            lambda status: status.get("network") == "testnet",
            description="the node's status route",
            timeout=STARTUP_TIMEOUT_SECONDS,
        )
        # The bootstrap window is mined without a drip: those blocks are the one
        # exception to the proof requirement. Wait for it before adding traffic,
        # so the first measured height already belongs to the live phase.
        _wait_for_json(
            f"{url}/v1/status",
            lambda status: (
                isinstance(status.get("height"), int)
                and status["height"] >= bootstrap_end_height(TESTNET)
            ),
            description=f"height {bootstrap_end_height(TESTNET)}",
            timeout=BOOTSTRAP_TIMEOUT_SECONDS,
        )
        yield url
    except BaseException:
        print(f"node logs before teardown:\n{_docker_logs(name)}")
        raise
    finally:
        _docker("rm", "--force", name, check=False)


@pytest.fixture(scope="session")
def drip(node_image: str, private_network: str, node: str) -> Iterator[str]:
    """Keep the quiet chain supplied with one ordinary transaction at a time.

    A block past bootstrap must carry a proof or exactly one transaction. The
    traffic generator alone is not enough: a refund waits for a height, and no
    block is built while its pool is empty. The drip is the document the stack's
    own ``drip`` service runs; the shell loop restarts it after a transient
    refusal, as the compose service's restart policy would.
    """
    del node  # only its readiness matters before the drip starts
    name = f"{private_network}-drip"
    supervisor = (
        "while :; do\n"
        f"  python /app/deploy/drip_testnet_traffic.py "
        f"--node http://{NODE_ALIAS}:{NODE_CONTAINER_PORT} --poll-seconds 0.2\n"
        "  sleep 0.2\n"
        "done\n"
    )
    _docker(
        "run",
        "--detach",
        "--rm",
        "--name",
        name,
        "--network",
        private_network,
        "--entrypoint",
        "sh",
        node_image,
        "-c",
        supervisor,
    )
    try:
        _wait_for_log(name, "dripped", timeout=STARTUP_TIMEOUT_SECONDS)
        yield name
    except BaseException:
        print(f"drip logs before teardown:\n{_docker_logs(name)}")
        raise
    finally:
        _docker("rm", "--force", name, check=False)


TRAFFIC_SCRIPT = """
import random

import httpx

from deploy import synthetic_testnet_traffic as traffic
from sccp.config import TESTNET

options = traffic.Options(
    # Fund a pool larger than the generator's ten-wallet seat cap. Cycle 2
    # refunds the HTLC it locks, and the bridge skips wallets with a pending
    # spend; if one of cycle 2's ten transfers is still unconfirmed, every seated
    # wallet is excluded. Two off-seat wallets then remain as the lock's sender
    # and beneficiary, which is what the refund needs.
    max_wallets=12,
    grant_coins=100,
    min_balance_coins=50,
    max_transfer_coins=1,
    idle_min_seconds=0.0,
    idle_max_seconds=0.0,
    wait_seconds=120.0,
    poll_seconds=0.2,
    extra_miners=0,
    bridge=True,
    dex=True,
    bridge_coins=1,
    bridge_claim_blocks=12,
    bridge_refund_blocks=2,
    dex_coins=1,
    node="http://node:8645",
)
with httpx.Client(base_url="http://node:8645", timeout=30.0) as client:
    reports = traffic.run(
        client,
        options=options,
        params=TESTNET,
        cycles=2,
        rng=random.Random(7),
    )
print(f"completed {len(reports)} cycle(s)")
"""


def _run_traffic(node_image: str, private_network: str) -> str:
    """Run two traffic cycles in a container and return its combined output."""
    name = f"{private_network}-traffic"
    try:
        completed = _docker(
            "run",
            "--rm",
            "--name",
            name,
            "--network",
            private_network,
            "--entrypoint",
            "python",
            node_image,
            "-c",
            TRAFFIC_SCRIPT,
            timeout=TRAFFIC_TIMEOUT_SECONDS,
        )
    finally:
        _docker("rm", "--force", name, check=False)
    return f"{completed.stdout}\n{completed.stderr}"


def _all_htlcs_closed(document: dict[str, Any]) -> bool:
    """Whether the API reports at least one claim and one refund, none open."""
    rows = document.get("htlcs")
    if not isinstance(rows, list):
        return False
    statuses = {row.get("status") for row in rows if isinstance(row, dict)}
    return {"claimed", "refunded"} <= statuses and "locked" not in statuses


def test_image_mines_and_closes_htlcs(
    node: str,
    drip: str,
    node_image: str,
    private_network: str,
) -> None:
    """The built image mines the traffic container's transfers and HTLC closes."""
    del drip  # its lifetime and readiness are the fixture's contract
    with httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS) as client:
        before = client.get(f"{node}/v1/status").json()
    start_height = before["height"]
    assert isinstance(start_height, int)

    output = _run_traffic(node_image, private_network)
    print(output)
    assert "completed 2 cycle(s)" in output, output
    assert "cycle 2:" in output, output
    # The traffic generator reaches the DEX every cycle. A testnet funded only
    # by the faucet mints no C960, so the pool list must stay empty; the skip
    # line proves the DEX branch ran instead of being bypassed.
    assert "dex: skipped" in output, output

    after = _wait_for_json(
        f"{node}/v1/status",
        lambda status: isinstance(status.get("height"), int) and status["height"] > start_height,
        description="a block past the pre-traffic tip",
        timeout=STARTUP_TIMEOUT_SECONDS,
    )
    assert after["network"] == "testnet"
    assert after["height"] > bootstrap_end_height(TESTNET)

    document = _wait_for_json(
        f"{node}/v1/htlcs",
        _all_htlcs_closed,
        description="a claimed and a refunded HTLC",
        timeout=STARTUP_TIMEOUT_SECONDS,
    )
    rows = document["htlcs"]
    assert document["network"] == "testnet"
    assert isinstance(rows, list)
    assert len(rows) >= 2
    for view in rows:
        assert view["network"] == "testnet"
        assert validate_address(view["sender"], TESTNET)
        assert validate_address(view["beneficiary"], TESTNET)
        assert view["sender"] != view["beneficiary"]
        assert view["amount"] == coins_to_units(1)
        assert view["created_at_height"] < view["timelock_height"]
        assert isinstance(view["closed_at_height"], int)
        assert view["closed_at_height"] >= view["created_at_height"]

    claimed = [view for view in rows if view["status"] == "claimed"]
    refunded = [view for view in rows if view["status"] == "refunded"]
    assert claimed and refunded
    for view in claimed:
        assert view["closed_at_height"] < view["timelock_height"]
        assert isinstance(view["preimage"], str) and len(view["preimage"]) == 64
    for view in refunded:
        assert view["closed_at_height"] >= view["timelock_height"]
        assert view["preimage"] is None

    with httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS) as client:
        pools = client.get(f"{node}/v1/pools")
    assert pools.status_code == 200
    assert pools.json() == {"network": "testnet", "pools": []}
