"""Build the image and exercise a live mining node with real side traffic.

The rest of the end-to-end suite runs the Python packages directly. This test
starts from the artefact a deployment actually ships: it builds the current
working tree with ``docker build``, creates a private network, and runs the
node, the traffic generator and the drip as separate containers. The node mines
on its own, the drip keeps a quiet chain supplied with ledger work, and the
traffic generator sends transfers and locks, claims and refunds HTLCs. Separate
cases assert that an ordinary transfer moves the balances the node reports, and
that the DEX can create a pool, swap, add liquidity and remove it again. A
payout case mines a maturity window of fee-paying transfers and checks that the
miner's account receives the bootstrap subsidy and fees, matures them on
schedule, and keeps the node's minted total in step. C960 only exists after a
settled Chess960 proof, so the DEX fixture credits the published traffic wallets
before its dedicated devnet node serves.

It is opt-in because it needs a Docker daemon and a few minutes of wall clock.
Set ``SCCP_RUN_CONTAINER_E2E=1`` and select the ``container`` marker::

    SCCP_RUN_CONTAINER_E2E=1 pytest -m container tests/e2e/test_container_smoke.py -s

``make container-e2e`` does the same. Set ``SCCP_CONTAINER_E2E_IMAGE`` to an
already-built tag to skip the build while iterating on the test itself.
"""

from __future__ import annotations

import json
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
from deploy import synthetic_testnet_traffic as traffic

from sccp.chain import dex
from sccp.chain.block import bootstrap_end_height
from sccp.config import CHESS960_ASSET_ID, DEVNET, TESTNET, coins_to_units
from sccp.crypto.address import encode_address, validate_address
from sccp.crypto.pq import hybrid_keypair_from_seed

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

NODE_CONTAINER_PORT = 8645
NODE_ALIAS = "node"
DEVNET_NODE_ALIAS = "devnet-node"

MINER_SEED = b"\x5c" * 32
MINER_ADDRESS = encode_address(hybrid_keypair_from_seed(MINER_SEED).public_key_bytes, TESTNET)
DEVNET_MINER_ADDRESS = encode_address(hybrid_keypair_from_seed(MINER_SEED).public_key_bytes, DEVNET)

SEEDED_WALLET_COUNT = 12
"""Traffic wallets both node fixtures seat; the DEX cases use the first two."""

TRAFFIC_SEED_COINS = 100
"""Native coins each testnet traffic wallet receives before the drip starts."""

SEEDED_NATIVE_COINS = 1_000
SEEDED_VARIANT_COINS = 1_000

TRANSFER_SENDER_INDEX = 10
TRANSFER_RECIPIENT_INDEX = 11
MINER_PAYOUT_SENDER_INDEX = 2
MINER_PAYOUT_RECIPIENT_INDEX = 3
DEX_CREATOR_INDEX = 0
DEX_TRADER_INDEX = 1
DEX_POOL_ID = dex.pool_id(dex.NATIVE_ASSET, CHESS960_ASSET_ID)

TRANSFER_COINS = 5
MINER_PAYOUT_COINS = 1
DEX_CREATE_COINS = 100
DEX_SWAP_COINS = 5
DEX_ADD_COINS = 10

MINER_PAYOUT_BLOCKS = DEVNET.coinbase_maturity + 1
"""One block past maturity: releases an old subsidy and a new fee reward."""

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


def _height(url: str) -> int:
    """The node's adopted tip height."""
    with httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS) as client:
        status = client.get(f"{url}/v1/status").json()
    height = status.get("height")
    assert isinstance(height, int) and not isinstance(height, bool)
    return height


def _marker_json(output: str, marker: str) -> dict[str, Any]:
    """The object on the last line beginning with ``marker``."""
    for line in reversed(output.splitlines()):
        if not line.startswith(marker):
            continue
        try:
            document = json.loads(line[len(marker) :])
        except json.JSONDecodeError as exc:
            pytest.fail(f"malformed {marker.strip()} output: {line}\n{exc}")
        if not isinstance(document, dict):
            pytest.fail(f"{marker.strip()} output is not an object: {line}")
        return document
    pytest.fail(f"no {marker.strip()} line in output:\n{output}")


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
        # Fund every traffic wallet in one run before the drip starts. Traffic
        # then never touches the faucet account, leaving its nonce to the drip.
        print(
            _run_script(
                node_image,
                private_network,
                label="seed-traffic",
                script=TRAFFIC_SEED_SCRIPT,
                timeout=BOOTSTRAP_TIMEOUT_SECONDS,
            )
        )
        yield url
    except BaseException:
        print(f"node logs before teardown:\n{_docker_logs(name)}")
        raise
    finally:
        _docker("rm", "--force", name, check=False)


def _seeded_devnet_node_script() -> str:
    """The in-container devnet node, with C960 credited before it starts serving.

    A settled Chess960 proof is the only consensus source of C960 and neither
    genesis nor the faucet mints it. The smoke test is about the transfer and DEX
    state transitions, not about playing a whole Chess960 match in the image, so
    the fixture credits the deterministic traffic wallets directly. After this
    one-time seed every transaction still goes through normal admission and is
    applied by the production block builder and validator.
    """
    return f"""
import threading

import uvicorn

from deploy import synthetic_testnet_traffic as traffic
from sccp.config import CHESS960_ASSET_ID, DEVNET, coins_to_units
from sccp.node.app import create_app
from sccp.node.miner import NodeMiner
from sccp.node.service import NodeService

service = NodeService.open(DEVNET)
service.check_startup()
for index in range({SEEDED_WALLET_COUNT}):
    wallet = traffic.wallet(index, DEVNET)
    account = service.chain.state.ensure(wallet.address)
    account.public_key = wallet.keypair.public_key_hex
    account.balance = coins_to_units({SEEDED_NATIVE_COINS})
    account.assets[CHESS960_ASSET_ID] = coins_to_units({SEEDED_VARIANT_COINS})
service.chain.state.minted += coins_to_units({SEEDED_NATIVE_COINS}) * {SEEDED_WALLET_COUNT}
service.chain.state.minted_assets[CHESS960_ASSET_ID] = (
    coins_to_units({SEEDED_VARIANT_COINS}) * {SEEDED_WALLET_COUNT}
)
# The mempool copied the genesis projection when the service opened; rebase it
# so admission sees the seeded balances before the first block does.
service.mempool.reset(service.chain.state)

miner = NodeMiner(service, {DEVNET_MINER_ADDRESS!r}, mine_bootstrap=True)
stop = threading.Event()
threading.Thread(
    target=miner.run,
    kwargs={{
        "stop": stop,
        "poll_seconds": 0.2,
        "on_block": lambda block: print(f"mined {{block.header.height}}", flush=True),
    }},
    daemon=True,
).start()
uvicorn.run(create_app(service), host="0.0.0.0", port={NODE_CONTAINER_PORT})
"""


@pytest.fixture(scope="session")
def devnet_node(node_image: str, private_network: str) -> Iterator[str]:
    """A seeded devnet node for the transfer and DEX smoke cases.

    It starts on its own alias rather than ``node`` so it can share the run's
    private network with the testnet node instead of racing it in DNS.
    """
    name = f"{private_network}-devnet-node"
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
        DEVNET_NODE_ALIAS,
        "--publish",
        published,
        "--entrypoint",
        "python",
        node_image,
        "-c",
        _seeded_devnet_node_script(),
    )
    url = f"http://127.0.0.1:{host_port}"
    try:
        _wait_for_json(
            f"{url}/v1/status",
            lambda status: status.get("network") == "devnet",
            description="the devnet node's status route",
            timeout=STARTUP_TIMEOUT_SECONDS,
        )
        _wait_for_json(
            f"{url}/v1/status",
            lambda status: (
                isinstance(status.get("height"), int)
                and status["height"] >= bootstrap_end_height(DEVNET)
            ),
            description=f"height {bootstrap_end_height(DEVNET)}",
            timeout=BOOTSTRAP_TIMEOUT_SECONDS,
        )
        yield url
    except BaseException:
        print(f"devnet node logs before teardown:\n{_docker_logs(name)}")
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
    # The node fixture funds all twelve wallets before the drip starts. Keeping
    # the top-up threshold at zero means traffic never spends from the faucet
    # account, which the drip uses for heartbeat blocks: two clients numbering
    # the same account can otherwise collide under a fast miner.
    min_balance_coins=0,
    max_transfer_coins=1,
    idle_min_seconds=0.0,
    idle_max_seconds=0.0,
    wait_seconds=120.0,
    poll_seconds=0.2,
    extra_miners=0,
    bridge=True,
    # The C960 pool has its own seeded devnet case below; a testnet faucet only
    # mints ZQWX, so this run leaves the DEX branch off.
    dex=False,
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


TRAFFIC_SEED_SCRIPT = f"""
import httpx

from deploy import synthetic_testnet_traffic as traffic
from sccp.chain.genesis import faucet_keypair
from sccp.config import TESTNET, coins_to_units
from sccp.crypto.address import encode_address

FAUCET = faucet_keypair(TESTNET.network_id)
FAUCET_WALLET = traffic.Wallet(
    index=-1,
    keypair=FAUCET,
    address=encode_address(FAUCET.public_key_bytes, TESTNET),
)

with httpx.Client(base_url="http://{NODE_ALIAS}:{NODE_CONTAINER_PORT}", timeout=30.0) as client:
    for index in range({SEEDED_WALLET_COUNT}):
        recipient = traffic.wallet(index, TESTNET)
        tx = traffic.transfer_transaction(
            FAUCET_WALLET,
            to=recipient.address,
            amount=coins_to_units({TRAFFIC_SEED_COINS}),
            nonce=traffic.nonce(client, FAUCET_WALLET.address),
            params=TESTNET,
        )
        digest = traffic.submit_transaction(client, tx)
        confirmed = traffic.wait_for_transactions(
            client,
            (digest,),
            wait_seconds=60.0,
            poll_seconds=0.2,
        )
        if digest not in confirmed:
            status = client.get("/v1/status").json()
            pool = client.get("/v1/mempool").json()
            raise SystemExit(
                f"funding {{digest}} did not confirm; status={{status}} pool={{pool}}"
            )
    print(f"funded {SEEDED_WALLET_COUNT} traffic wallets", flush=True)
"""


TRANSFER_SCRIPT = f"""
import json

import httpx

from deploy import synthetic_testnet_traffic as traffic
from sccp.config import DEVNET, coins_to_units

SENDER = traffic.wallet({TRANSFER_SENDER_INDEX}, DEVNET)
RECIPIENT = traffic.wallet({TRANSFER_RECIPIENT_INDEX}, DEVNET)
AMOUNT = coins_to_units({TRANSFER_COINS})

with httpx.Client(
    base_url="http://{DEVNET_NODE_ALIAS}:{NODE_CONTAINER_PORT}", timeout=30.0
) as client:
    sender_before = traffic.account(client, SENDER.address)
    recipient_before = traffic.account(client, RECIPIENT.address)
    tx = traffic.transfer_transaction(
        SENDER,
        to=RECIPIENT.address,
        amount=AMOUNT,
        nonce=int(sender_before["nonce"]),
        params=DEVNET,
    )
    digest = traffic.submit_transaction(client, tx)
    confirmed = traffic.wait_for_transactions(
        client,
        (digest,),
        wait_seconds=120.0,
        poll_seconds=0.2,
    )
    response = client.get(f"/v1/transactions/{{digest}}")
    response.raise_for_status()
    record = response.json()
    sender_after = traffic.account(client, SENDER.address)
    recipient_after = traffic.account(client, RECIPIENT.address)
    print(
        "TRANSFER_RESULT "
        + json.dumps(
            {{
                "txid": digest,
                "confirmed": digest in confirmed,
                "height": record.get("height"),
                "amount": AMOUNT,
                "fee": DEVNET.min_fee,
                "sender": SENDER.address,
                "recipient": RECIPIENT.address,
                "sender_before": int(sender_before["balance"]),
                "sender_after": int(sender_after["balance"]),
                "recipient_before": int(recipient_before["balance"]),
                "recipient_after": int(recipient_after["balance"]),
            }}
        ),
        flush=True,
    )
    if digest not in confirmed:
        raise SystemExit(f"transfer {{digest}} did not confirm")
"""

MINER_PAYOUT_SCRIPT = f"""
import json

import httpx

from deploy import synthetic_testnet_traffic as traffic
from sccp.config import DEVNET, coins_to_units

MINER = {DEVNET_MINER_ADDRESS!r}
SENDER = traffic.wallet({MINER_PAYOUT_SENDER_INDEX}, DEVNET)
RECIPIENT = traffic.wallet({MINER_PAYOUT_RECIPIENT_INDEX}, DEVNET)
AMOUNT = coins_to_units({MINER_PAYOUT_COINS})

with httpx.Client(
    base_url="http://{DEVNET_NODE_ALIAS}:{NODE_CONTAINER_PORT}", timeout=30.0
) as client:
    before = traffic.account(client, MINER)
    status_before = client.get("/v1/status").json()
    start_height = traffic.tip_height(client)
    txids = []
    for index in range({MINER_PAYOUT_BLOCKS}):
        tx = traffic.transfer_transaction(
            SENDER,
            to=RECIPIENT.address,
            amount=AMOUNT,
            nonce=traffic.nonce(client, SENDER.address),
            params=DEVNET,
        )
        digest = traffic.submit_transaction(client, tx)
        confirmed = traffic.wait_for_transactions(
            client, (digest,), wait_seconds=60.0, poll_seconds=0.2
        )
        if digest not in confirmed:
            status = client.get("/v1/status").json()
            pool = client.get("/v1/mempool").json()
            raise SystemExit(
                f"payout transaction {{index}} ({{digest}}) did not confirm; "
                f"status={{status}} pool={{pool}}"
            )
        txids.append(digest)
    end_height = traffic.tip_height(client)
    after = traffic.account(client, MINER)
    status_after = client.get("/v1/status").json()
    print(
        "MINER_PAYOUT_RESULT "
        + json.dumps(
            {{
                "miner": MINER,
                "sender": SENDER.address,
                "recipient": RECIPIENT.address,
                "amount": AMOUNT,
                "fee": DEVNET.min_fee,
                "blocks": {MINER_PAYOUT_BLOCKS},
                "start_height": start_height,
                "end_height": end_height,
                "txids": txids,
                "before": {{
                    "balance": int(before["balance"]),
                    "immature": int(before["immature"]),
                    "total": int(before["total"]),
                }},
                "after": {{
                    "balance": int(after["balance"]),
                    "immature": int(after["immature"]),
                    "total": int(after["total"]),
                }},
                "minted_before": int(status_before["minted"]),
                "minted_after": int(status_after["minted"]),
            }}
        ),
        flush=True,
    )
"""

DEX_SCRIPT = f"""
import json

import httpx

from deploy import synthetic_testnet_traffic as traffic
from sccp.chain import dex
from sccp.config import CHESS960_ASSET_ID, DEVNET, coins_to_units

POOL_ID = dex.pool_id(dex.NATIVE_ASSET, CHESS960_ASSET_ID)
CREATOR = traffic.wallet({DEX_CREATOR_INDEX}, DEVNET)
TRADER = traffic.wallet({DEX_TRADER_INDEX}, DEVNET)


def submit(client, tx):
    \"\"\"Submit one signed transaction and wait until a block holds it.\"\"\"
    digest = traffic.submit_transaction(client, tx)
    confirmed = traffic.wait_for_transactions(
        client, (digest,), wait_seconds=120.0, poll_seconds=0.2
    )
    if digest not in confirmed:
        raise SystemExit(f"transaction {{digest}} did not confirm")
    return digest


def pool(client):
    \"\"\"The C960 pool, which must have been created by this script.\"\"\"
    rows = {{row["pool_id"]: row for row in traffic.pool_views(client)}}
    try:
        return rows[POOL_ID]
    except KeyError:
        raise SystemExit(f"pool {{POOL_ID}} is missing") from None


with httpx.Client(
    base_url="http://{DEVNET_NODE_ALIAS}:{NODE_CONTAINER_PORT}", timeout=30.0
) as client:
    if traffic.pool_views(client):
        raise SystemExit("the seeded node already has a pool; the DEX test needs a fresh one")

    created_amount = coins_to_units({DEX_CREATE_COINS})
    create_txid = submit(
        client,
        traffic.create_pool_transaction(
            CREATOR,
            asset=CHESS960_ASSET_ID,
            amount=created_amount,
            nonce=traffic.nonce(client, CREATOR.address),
            params=DEVNET,
        ),
    )
    after_create = pool(client)

    swap_in = coins_to_units({DEX_SWAP_COINS})
    quote = dex.swap_output(
        after_create["reserve_a"],
        after_create["reserve_b"],
        swap_in,
        after_create["fee_bps"],
    )
    trader_before = traffic.account(client, TRADER.address)
    swap_txid = submit(
        client,
        traffic.swap_transaction(
            TRADER,
            pool_id=POOL_ID,
            asset_in=dex.NATIVE_ASSET,
            amount_in=swap_in,
            min_amount_out=quote * 9950 // 10000,
            nonce=traffic.nonce(client, TRADER.address),
            params=DEVNET,
        ),
    )
    after_swap = pool(client)
    trader_after = traffic.account(client, TRADER.address)

    deposit_a = coins_to_units({DEX_ADD_COINS})
    deposit_b = dex.proportional_amount(
        deposit_a, after_swap["reserve_a"], after_swap["reserve_b"]
    )
    add_txid = submit(
        client,
        traffic.add_liquidity_transaction(
            CREATOR,
            pool_id=POOL_ID,
            amount_a=deposit_a,
            amount_b=deposit_b,
            nonce=traffic.nonce(client, CREATOR.address),
            params=DEVNET,
        ),
    )
    after_add = pool(client)

    owned = after_add["shares"].get(CREATOR.address, 0)
    shares = max(1, owned // 10)
    payout_a, payout_b = dex.removed_amounts(
        after_add["reserve_a"],
        after_add["reserve_b"],
        after_add["total_shares"],
        shares,
    )
    remove_txid = submit(
        client,
        traffic.remove_liquidity_transaction(
            CREATOR,
            pool_id=POOL_ID,
            shares=shares,
            nonce=traffic.nonce(client, CREATOR.address),
            params=DEVNET,
        ),
    )
    final = pool(client)

    print(
        "DEX_RESULT "
        + json.dumps(
            {{
                "pool_id": POOL_ID,
                "create": {{"txid": create_txid, "pool": after_create}},
                "swap": {{
                    "txid": swap_txid,
                    "amount_in": swap_in,
                    "quote": quote,
                    "pool": after_swap,
                    "trader_before": int(trader_before["assets"][CHESS960_ASSET_ID]),
                    "trader_after": int(trader_after["assets"][CHESS960_ASSET_ID]),
                }},
                "add": {{
                    "txid": add_txid,
                    "amount_a": deposit_a,
                    "amount_b": deposit_b,
                    "pool": after_add,
                }},
                "remove": {{
                    "txid": remove_txid,
                    "shares": shares,
                    "payout_a": payout_a,
                    "payout_b": payout_b,
                    "pool": final,
                }},
            }}
        ),
        flush=True,
    )
"""


def _run_script(
    node_image: str,
    private_network: str,
    *,
    label: str,
    script: str,
    timeout: float = TRAFFIC_TIMEOUT_SECONDS,
) -> str:
    """Run one Python program in the image and return its combined output."""
    name = f"{private_network}-{label}"
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
            script,
            timeout=timeout,
        )
    finally:
        _docker("rm", "--force", name, check=False)
    return f"{completed.stdout}\n{completed.stderr}"


def _run_traffic(node_image: str, private_network: str) -> str:
    """Run two traffic cycles in a container and return its combined output."""
    return _run_script(
        node_image,
        private_network,
        label="traffic",
        script=TRAFFIC_SCRIPT,
    )


def _run_transfer(node_image: str, private_network: str) -> str:
    """Send one ordinary transfer from the image's wallet code."""
    return _run_script(
        node_image,
        private_network,
        label="transfer",
        script=TRANSFER_SCRIPT,
        timeout=BOOTSTRAP_TIMEOUT_SECONDS,
    )


def _run_miner_payout(node_image: str, private_network: str) -> str:
    """Mine a maturity window of fee-paying transfers from the image."""
    return _run_script(
        node_image,
        private_network,
        label="miner-payout",
        script=MINER_PAYOUT_SCRIPT,
        timeout=BOOTSTRAP_TIMEOUT_SECONDS,
    )


def _run_dex(node_image: str, private_network: str) -> str:
    """Create a C960 pool and exercise all three pool actions."""
    return _run_script(
        node_image,
        private_network,
        label="dex",
        script=DEX_SCRIPT,
    )


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


def test_image_transfers_coins(
    devnet_node: str,
    node_image: str,
    private_network: str,
) -> None:
    """The built image sends an ordinary transfer that a block settles."""
    sender = traffic.wallet(TRANSFER_SENDER_INDEX, DEVNET)
    recipient = traffic.wallet(TRANSFER_RECIPIENT_INDEX, DEVNET)
    start_height = _height(devnet_node)

    output = _run_transfer(node_image, private_network)
    print(output)
    result = _marker_json(output, "TRANSFER_RESULT ")

    assert result["confirmed"] is True
    assert result["sender"] == sender.address
    assert result["recipient"] == recipient.address
    assert result["amount"] == coins_to_units(TRANSFER_COINS)
    assert result["fee"] == DEVNET.min_fee
    height = result["height"]
    assert isinstance(height, int) and height >= start_height
    assert result["sender_after"] == result["sender_before"] - result["amount"] - result["fee"]
    assert result["recipient_after"] == result["recipient_before"] + result["amount"]

    with httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS) as client:
        record = client.get(f"{devnet_node}/v1/transactions/{result['txid']}").json()
    assert record["type"] == "transfer"
    assert record["height"] == height
    assert record["in_mempool"] is False


def test_image_pays_the_miner(
    devnet_node: str,
    node_image: str,
    private_network: str,
) -> None:
    """Subsidies, fees and maturity reach the miner account the API reports."""
    output = _run_miner_payout(node_image, private_network)
    print(output)
    result = _marker_json(output, "MINER_PAYOUT_RESULT ")

    assert result["miner"] == DEVNET_MINER_ADDRESS
    assert result["sender"] == traffic.wallet(MINER_PAYOUT_SENDER_INDEX, DEVNET).address
    assert result["recipient"] == traffic.wallet(MINER_PAYOUT_RECIPIENT_INDEX, DEVNET).address
    assert result["amount"] == coins_to_units(MINER_PAYOUT_COINS)
    assert result["fee"] == DEVNET.min_fee
    assert result["blocks"] == MINER_PAYOUT_BLOCKS
    assert len(result["txids"]) == MINER_PAYOUT_BLOCKS

    start_height = result["start_height"]
    end_height = result["end_height"]
    assert isinstance(start_height, int) and not isinstance(start_height, bool)
    assert isinstance(end_height, int) and not isinstance(end_height, bool)
    assert end_height == start_height + MINER_PAYOUT_BLOCKS

    with httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS) as client:
        blocks = {
            height: client.get(f"{devnet_node}/v1/blocks/{height}").json()["block"]
            for height in range(1, end_height + 1)
        }

    def coinbase(height: int) -> int:
        block = blocks[height]
        assert block["header"]["miner"] == DEVNET_MINER_ADDRESS
        assert block["coinbase"]["payload"]["to"] == DEVNET_MINER_ADDRESS
        amount = block["coinbase"]["payload"]["amount"]
        assert isinstance(amount, int) and not isinstance(amount, bool)
        return amount

    bootstrap_end = bootstrap_end_height(DEVNET)
    for height in range(1, bootstrap_end + 1):
        assert blocks[height]["transactions"] == []
        assert blocks[height]["proofs"] == []
        assert coinbase(height) == DEVNET.subsidy

    for height in range(bootstrap_end + 1, end_height + 1):
        block = blocks[height]
        assert block["proofs"] == []
        assert len(block["transactions"]) == 1
        fees = sum(tx["fee"] for tx in block["transactions"])
        assert coinbase(height) == fees
    for height in range(start_height + 1, end_height + 1):
        assert coinbase(height) == DEVNET.min_fee

    maturity = DEVNET.coinbase_maturity
    released_before = sum(coinbase(height) for height in range(1, start_height - maturity + 1))
    released_after = sum(coinbase(height) for height in range(1, end_height - maturity + 1))
    new_value = sum(coinbase(height) for height in range(start_height + 1, end_height + 1))
    matured = released_after - released_before

    before = result["before"]
    after = result["after"]
    assert matured > 0
    assert after["balance"] - before["balance"] == matured
    assert after["immature"] - before["immature"] == new_value - matured
    assert after["total"] - before["total"] == new_value
    assert result["minted_after"] - result["minted_before"] == new_value


def test_image_exercises_dex(
    devnet_node: str,
    node_image: str,
    private_network: str,
) -> None:
    """The built image creates, swaps through and drains a C960 pool."""
    creator = traffic.wallet(DEX_CREATOR_INDEX, DEVNET)
    start_height = _height(devnet_node)

    output = _run_dex(node_image, private_network)
    print(output)
    result = _marker_json(output, "DEX_RESULT ")

    assert result["pool_id"] == DEX_POOL_ID
    created = result["create"]["pool"]
    assert created["network"] == "devnet"
    assert created["asset_a"] is None
    assert created["asset_b"] == CHESS960_ASSET_ID
    assert created["reserve_a"] == created["reserve_b"] == coins_to_units(DEX_CREATE_COINS)
    assert created["total_shares"] > 0
    assert created["shares"][creator.address] == created["total_shares"]
    assert created["fee_bps"] == DEVNET.dex_fee_bps

    swap = result["swap"]
    swapped = swap["pool"]
    assert swap["amount_in"] == coins_to_units(DEX_SWAP_COINS)
    assert swap["quote"] > 0
    assert swapped["reserve_a"] == created["reserve_a"] + swap["amount_in"]
    assert swapped["reserve_b"] == created["reserve_b"] - swap["quote"]
    assert swapped["total_shares"] == created["total_shares"]
    assert swap["trader_after"] - swap["trader_before"] == swap["quote"]

    add = result["add"]
    added = add["pool"]
    assert add["amount_a"] > 0 and add["amount_b"] > 0
    assert added["reserve_a"] == swapped["reserve_a"] + add["amount_a"]
    assert added["reserve_b"] == swapped["reserve_b"] + add["amount_b"]
    assert added["total_shares"] > swapped["total_shares"]
    assert added["shares"][creator.address] > created["shares"][creator.address]

    remove = result["remove"]
    final = remove["pool"]
    assert remove["shares"] > 0
    assert final["reserve_a"] == added["reserve_a"] - remove["payout_a"]
    assert final["reserve_b"] == added["reserve_b"] - remove["payout_b"]
    assert final["total_shares"] == added["total_shares"] - remove["shares"]
    assert final["shares"][creator.address] == added["shares"][creator.address] - remove["shares"]

    steps = (
        ("create", "create_pool"),
        ("swap", "swap"),
        ("add", "add_liquidity"),
        ("remove", "remove_liquidity"),
    )
    with httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS) as client:
        for key, kind in steps:
            record = client.get(f"{devnet_node}/v1/transactions/{result[key]['txid']}").json()
            assert record["type"] == kind
            height = record["height"]
            assert isinstance(height, int) and height >= start_height
            assert record["in_mempool"] is False
        pool = client.get(f"{devnet_node}/v1/pools/{DEX_POOL_ID}").json()
    assert pool["reserve_a"] == final["reserve_a"]
    assert pool["reserve_b"] == final["reserve_b"]
    assert pool["total_shares"] == final["total_shares"]
