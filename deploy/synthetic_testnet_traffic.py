"""Drive realistic wallet traffic through a running testnet node.

``drip_testnet_traffic`` keeps an epoch supplied with the one piece of ledger work
every block needs, but it only acts on an *empty* pool with a single faucet
transfer: it keeps the chain moving, it does not exercise what a user does. This
script does. It creates its whole pool on startup, then every cycle it

1. picks 2-10 wallets from that pool (a random count),
2. funds each one from the faucet, topping up only those that are short,
3. waits for those grants to be mined,
4. sends 1-10 transfers between the wallets,
5. waits for those transfers to be mined,
6. exercises one bridge leg (an HTLC lock and claim, or a lock and refund)
   and one DEX action (create a pool, add or remove liquidity, or swap),
7. waits for that side traffic to settle, then
8. idles for 1-5 minutes before starting over.

Wallets come from fixed seeds, so a restart seats the same addresses and nothing
has to be persisted. That matters because the redeploy workflow runs
``docker compose down -v`` and rebuilds the chain from genesis: a stored wallet
file would be stale the moment it restarts.

Two properties keep the loop solvent and accepted by consensus:

* transfers run *between* the pool's own wallets, so the coins circulate and the
  faucet is tapped only for new wallets and for fees (1,000 units each, next to
  nothing against the genesis faucet's 1,000,000 coins);
* every sender's nonce is tracked locally. ``GET /v1/accounts`` reports committed
  state, not the mempool projection, so several grants or transfers from one
  account must be numbered by the client -- the same rule ``docs/operations/RUNBOOK.md``
  gives an operator sending two transactions in a row.

Only this cycle's own transactions are waited on, never an empty pool:
``drip_testnet_traffic`` refills a drained pool within a block, so a generator that
waited for the pool to empty would wait forever. That same drip spends the one
faucet account, and its transfer is invisible in ``GET /v1/accounts`` until a block
takes it, so a grant numbered from that read can collide with it. The collision is
retried until the next block frees the nonce or ``--wait-seconds`` runs out. A grant
that still does not confirm costs its wallet a cycle, not the whole cycle -- the
wallets funded before it send to each other meanwhile, which is the traffic this
script exists to make.

The public faucet allows five grants per client per hour and one per address, and
every request here leaves one container IP. A cycle that seats more wallets than
the budget therefore falls back to signing the same transfer with the published
faucet key, the operator path ``docs/operations/RUNBOOK.md`` documents for recovering a test
wallet. The fallback is logged, and the endpoint is tried again next cycle, so
the faucet itself stays in the loop whenever it has budget.

Two further kinds of activity ride along, because a deployed chain that only ever
sees well-behaved wallets never exercises them:

* ``--extra-miners`` seats that many ``sccp miner run`` children from fixed seeds
  (``synthetic_miner_creation``) and keeps them seated, so registrations,
  availability commitments and roster roles happen on a chain nobody is
  watching. The host's CPU and memory decide how many may actually run. The two
  seats have fixed jobs: one is deliberately slow, so its clock really runs out
  and the signed flag plus the bonded timeout claim are exercised; the other runs
  GNU Chess, so the roster is not a monoculture of one engine.

Those miners spend from the same faucet as the wallets, through the same gateway,
so all of a cycle's nonces are numbered in one place. They are one flag away from
off: ``--extra-miners 0`` leaves exactly the wallet traffic.

The bridge and DEX traffic is best-effort and reports what it did on the cycle
line. A bridge leg only needs native SCCP: the lock pays one wallet, the claim
reveals the hash preimage from another, and an alternate cycle waits out the
timelock and refunds instead. The DEX needs a pool or a variant token to seed one
with. Traffic wallets are funded from the native faucet, so a fresh chain with no
C960 holder has nothing to pair SCCP with: the generator reads ``GET /v1/pools``,
uses a pool another client created, and otherwise logs the skip rather than
failing the cycle. Tests seed the variant asset directly.

Usage::

    python deploy/synthetic_testnet_traffic.py --node http://node:8645

Like the drip, this is a testnet convenience: the faucet seed is published and
the coins have no value.
"""

from __future__ import annotations

import argparse
import hashlib
import random
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import httpx

from sccp.chain import dex
from sccp.chain.genesis import faucet_keypair
from sccp.chain.tx import htlc_id, sign_transaction, txid
from sccp.config import TESTNET, NetworkParams, coins_to_units, format_coins
from sccp.crypto.address import encode_address
from sccp.crypto.pq import HybridKeypair, hybrid_keypair_from_seed
from sccp.protocol.models import Transaction, TxType

try:  # the tests import this module as ``deploy.synthetic_testnet_traffic``
    from deploy import synthetic_miner_creation as miners
except ImportError:  # pragma: no cover - compose runs the file, not the package
    import synthetic_miner_creation as miners  # type: ignore[no-redef,import-not-found]

MIN_WALLETS: Final = 2
MAX_WALLETS: Final = 10
MIN_TRANSFERS: Final = 1
MAX_TRANSFERS: Final = 10
MIN_TRANSFER_COINS: Final = 1
MAX_TRANSFER_COINS: Final = 5
"""The largest single send. Small on purpose: the coins only have to circulate."""

GRANT_COINS: Final = 10
"""Coins per faucet grant. The faucet allows up to 1,000; ten is a wallet's worth of sends."""

MIN_BALANCE_COINS: Final = 3
"""Top a wallet up when it holds less than this, so it can always fund a send."""

IDLE_MIN_SECONDS: Final = 60.0
IDLE_MAX_SECONDS: Final = 300.0
WAIT_SECONDS: Final = 900.0
"""How long to wait for a block. The node settles one a minute; 900s is three misses."""

POLL_SECONDS: Final = 5.0
SEED_NAMESPACE: Final = b"sccp/testnet/traffic-wallet/"

EXTRA_MINERS: Final = 2
"""How many extra miners the pool holds: the slow seat and the alternate engine.

``0`` seats none and disables the role seating entirely.
"""

MAX_EXTRA_ONLINE: Final = 2
"""How many of that pool may run at once.

Both roles are permanent parts of the testnet: the slow seat is what produces a
real flag, and the alternate engine is what keeps the roster from being one
binary. The host budget still caps it -- on a two-vCPU box ``host_capacity``
allows exactly two, which is why the pool is this size.
"""

EXTRA_MINER_CHURN: Final = False
"""Whether the pool is meant to churn seats or to stay seated.

The churn is a demonstration of roster turnover; the testnet's two extra seats
are fixed roles, so they are started once and kept up.
"""

EXTRA_WITNESS_KEY_FILES: Final = (
    Path("/stack/keys/witness-1.pub"),
    Path("/stack/keys/witness-2.pub"),
    Path("/stack/keys/witness-3.pub"),
)
"""Every clock witness the extra seats trust, so a flag can become a claim.

``deploy/init_testnet_stack.py`` writes one file per witness. Missing files are
skipped: a stack without witnesses still plays, it just cannot settle a timeout.
"""

EXTRA_MINER_DIRECTORY: Final = Path("/data/synthetic-miners")
"""Where extra miners keep keyfiles. Inside the container on purpose: every
identity comes from a published seed, so nothing here has to outlive a redeploy."""

FLEET_STATUS_PATH: Final = Path("/stack/synthetic-fleet.json")
"""Where the fleet publishes the seats it holds.

``check_testnet_stack.py --fleet`` reads it after a deploy: the two extra miners
run as children of this container, so the only way to prove they came up is the
report they write here. ``/stack`` is already mounted, and the file is rewritten
at the start of every cycle so it also shows a seat that died and was repaired.
"""

BRIDGE_COINS: Final = 1
"""Coins in one HTLC. Small: the bridge traffic exercises the path, not the float."""

BRIDGE_CLAIM_BLOCKS: Final = 12
"""Blocks past the lock's own block before its timelock. The claim lands well inside it."""

BRIDGE_REFUND_BLOCKS: Final = 1
"""Blocks past the lock's own block before a refund becomes valid."""

DEX_COINS: Final = 1
"""Target size of a DEX side. Pool-first deposits are capped at one coin per side."""

DEX_SLIPPAGE_BPS: Final = 50
"""The minimum-output haircut signed into a synthetic swap (50 bp)."""

DEX_ACTIONS: Final = ("swap", "add", "remove")
"""The rotation a cycle with an existing pool walks: one action per cycle."""


class TrafficError(RuntimeError):
    """The generator could not read the node or submit a transaction."""


class FaucetRefused(TrafficError):
    """The public faucet refused one grant, usually its hourly rate limit."""


NONCE_CONFLICT: Final = "already has a pending transaction with nonce"
"""The node's refusal when the next nonce of an account is already in the pool."""


class NonceConflict(TrafficError):
    """The nonce a grant was built with is already pending, so the grant was not admitted.

    The faucet is one account shared with ``drip_testnet_traffic``, which sends into
    every empty pool. ``GET /v1/accounts`` reports the *committed* nonce, so a drip
    transfer the last block has not taken yet is invisible there; a grant numbered
    from that read collides with it. The generator retries instead of failing the
    cycle, because the collision clears as soon as that block is settled.
    """


@dataclass(frozen=True, slots=True)
class Wallet:
    """One deterministic testnet wallet the generator seats."""

    index: int
    keypair: HybridKeypair
    address: str


def wallet_seed(index: int) -> bytes:
    """The fixed, published seed for traffic wallet ``index``."""
    if index < 0:
        raise ValueError("a wallet index must be non-negative")
    return hashlib.sha256(SEED_NAMESPACE + index.to_bytes(4, "big")).digest()


def wallet(index: int, params: NetworkParams = TESTNET) -> Wallet:
    """Derive traffic wallet ``index``; the same index always yields the same key."""
    keypair = hybrid_keypair_from_seed(wallet_seed(index))
    return Wallet(
        index=index,
        keypair=keypair,
        address=encode_address(keypair.public_key_bytes, params),
    )


# -- talking to the node ------------------------------------------------------


def _get_object(client: httpx.Client, path: str) -> Mapping[str, object]:
    """GET ``path`` and return a JSON object, or raise :class:`TrafficError`."""
    response = client.get(path)
    if response.status_code >= 300:
        raise TrafficError(f"GET {path} failed: HTTP {response.status_code} {response.text}")
    payload = response.json()
    if not isinstance(payload, Mapping):
        raise TrafficError(f"GET {path} did not return an object")
    return payload


def _integer(value: object, field: str, context: str) -> int:
    """Return ``value`` as a non-negative integer, or raise :class:`TrafficError`."""
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise TrafficError(f"{context} reported an invalid {field}: {value!r}")
    return value


def account(client: httpx.Client, address: str) -> Mapping[str, object]:
    """The committed account view for ``address``."""
    return _get_object(client, f"/v1/accounts/{address}")


def balance(client: httpx.Client, address: str) -> int:
    """The spendable balance of ``address``, in base units."""
    return _integer(account(client, address).get("balance"), "balance", address)


def nonce(client: httpx.Client, address: str) -> int:
    """The next committed nonce of ``address``."""
    return _integer(account(client, address).get("nonce"), "nonce", address)


def account_asset_balance(view: Mapping[str, object], asset: str) -> int:
    """The committed balance of one trade asset in an account view.

    Native SCCP is the empty asset id and lives in ``balance``. A variant token
    lives in ``assets``; a view that omits the map (an older node) simply holds
    none of it.
    """
    if asset == dex.NATIVE_ASSET:
        return _integer(view.get("balance"), "balance", str(view.get("address")))
    assets = view.get("assets")
    if assets is None:
        return 0
    if not isinstance(assets, Mapping):
        raise TrafficError(f"account {view.get('address')} reported invalid assets: {assets!r}")
    return _integer(assets.get(asset, 0), asset, str(view.get("address")))


def tip_height(client: httpx.Client) -> int:
    """The adopted chain's tip height."""
    payload = _get_object(client, "/v1/status")
    return _integer(payload.get("height"), "height", "/v1/status")


def pool_views(client: httpx.Client) -> list[Mapping[str, object]]:
    """Every DEX pool the node serves, as raw ``GET /v1/pools`` rows."""
    payload = _get_object(client, "/v1/pools")
    pools = payload.get("pools")
    if pools is None:
        return []
    if not isinstance(pools, list) or not all(isinstance(row, Mapping) for row in pools):
        raise TrafficError("/v1/pools did not report pool objects")
    return [row for row in pools if isinstance(row, Mapping)]


def pending_txids(client: httpx.Client) -> set[str]:
    """The transaction ids the pool is holding right now."""
    payload = _get_object(client, "/v1/mempool")
    pending = payload.get("transactions")
    if not isinstance(pending, list) or not all(isinstance(item, str) for item in pending):
        raise TrafficError("/v1/mempool did not report transaction ids")
    return set(pending)


def submit_transaction(client: httpx.Client, tx: Transaction) -> str:
    """Submit one signed transaction; return its txid.

    A nonce the pool already holds is reported as :class:`NonceConflict` rather
    than a plain refusal: it is the one rejection a caller can wait out.
    """
    response = client.post("/v1/tx", json=tx.model_dump(mode="json"))
    if response.status_code >= 300:
        detail = response.text
        if response.status_code == 400 and NONCE_CONFLICT in detail:
            raise NonceConflict(f"nonce {tx.nonce} is already pending: {detail}")
        raise TrafficError(f"the node refused the transfer: HTTP {response.status_code} {detail}")
    payload = response.json()
    digest = payload.get("txid") if isinstance(payload, Mapping) else None
    return digest if isinstance(digest, str) and digest else txid(tx)


def transfer_transaction(
    sender: Wallet,
    *,
    to: str,
    amount: int,
    nonce: int,
    params: NetworkParams = TESTNET,
) -> Transaction:
    """Build and sign the transfer a wallet would make; the node still validates it."""
    tx = Transaction(
        network=params.network_id,
        type=TxType.TRANSFER,
        sender=sender.address,
        nonce=nonce,
        fee=params.min_fee,
        payload={
            "to": to,
            "amount": amount,
            "public_key": sender.keypair.public_key_hex,
        },
    )
    return sign_transaction(sender.keypair, tx)


def create_pool_transaction(
    sender: Wallet,
    *,
    asset: str,
    amount: int,
    nonce: int,
    params: NetworkParams = TESTNET,
) -> Transaction:
    """Build and sign a pool creation pairing native SCCP with ``asset``."""
    tx = Transaction(
        network=params.network_id,
        type=TxType.CREATE_POOL,
        sender=sender.address,
        nonce=nonce,
        fee=params.min_fee,
        payload={
            "asset_b": asset,
            "amount_a": amount,
            "amount_b": amount,
            "public_key": sender.keypair.public_key_hex,
        },
    )
    return sign_transaction(sender.keypair, tx)


def add_liquidity_transaction(
    sender: Wallet,
    *,
    pool_id: str,
    amount_a: int,
    amount_b: int,
    nonce: int,
    params: NetworkParams = TESTNET,
) -> Transaction:
    """Build and sign a two-sided deposit into ``pool_id``."""
    tx = Transaction(
        network=params.network_id,
        type=TxType.ADD_LIQUIDITY,
        sender=sender.address,
        nonce=nonce,
        fee=params.min_fee,
        payload={
            "pool_id": pool_id,
            "amount_a": amount_a,
            "amount_b": amount_b,
            "public_key": sender.keypair.public_key_hex,
        },
    )
    return sign_transaction(sender.keypair, tx)


def remove_liquidity_transaction(
    sender: Wallet,
    *,
    pool_id: str,
    shares: int,
    nonce: int,
    params: NetworkParams = TESTNET,
) -> Transaction:
    """Build and sign a withdrawal of ``shares`` from ``pool_id``."""
    tx = Transaction(
        network=params.network_id,
        type=TxType.REMOVE_LIQUIDITY,
        sender=sender.address,
        nonce=nonce,
        fee=params.min_fee,
        payload={
            "pool_id": pool_id,
            "shares": shares,
            "public_key": sender.keypair.public_key_hex,
        },
    )
    return sign_transaction(sender.keypair, tx)


def swap_transaction(
    sender: Wallet,
    *,
    pool_id: str,
    asset_in: str,
    amount_in: int,
    min_amount_out: int,
    nonce: int,
    params: NetworkParams = TESTNET,
) -> Transaction:
    """Build and sign an exact-input swap against ``pool_id``.

    Native SCCP is the empty asset id, and the node's schema distinguishes it by
    *omitting* ``asset_in``; an empty string is not an asset id.
    """
    payload: dict[str, object] = {
        "pool_id": pool_id,
        "amount_in": amount_in,
        "min_amount_out": min_amount_out,
        "public_key": sender.keypair.public_key_hex,
    }
    if asset_in:
        payload["asset_in"] = asset_in
    tx = Transaction(
        network=params.network_id,
        type=TxType.SWAP,
        sender=sender.address,
        nonce=nonce,
        fee=params.min_fee,
        payload=payload,
    )
    return sign_transaction(sender.keypair, tx)


def htlc_lock_transaction(
    sender: Wallet,
    *,
    beneficiary: str,
    amount: int,
    hashlock: str,
    timelock_height: int,
    nonce: int,
    params: NetworkParams = TESTNET,
) -> Transaction:
    """Build and sign a native-SCCP HTLC lock."""
    tx = Transaction(
        network=params.network_id,
        type=TxType.HTLC_LOCK,
        sender=sender.address,
        nonce=nonce,
        fee=params.min_fee,
        payload={
            "beneficiary": beneficiary,
            "amount": amount,
            "hashlock": hashlock,
            "timelock_height": timelock_height,
            "public_key": sender.keypair.public_key_hex,
        },
    )
    return sign_transaction(sender.keypair, tx)


def htlc_claim_transaction(
    claimant: Wallet,
    *,
    htlc: str,
    preimage: str,
    nonce: int,
    params: NetworkParams = TESTNET,
) -> Transaction:
    """Build and sign a claim revealing ``preimage``."""
    tx = Transaction(
        network=params.network_id,
        type=TxType.HTLC_CLAIM,
        sender=claimant.address,
        nonce=nonce,
        fee=params.min_fee,
        payload={
            "htlc_id": htlc,
            "preimage": preimage,
            "public_key": claimant.keypair.public_key_hex,
        },
    )
    return sign_transaction(claimant.keypair, tx)


def htlc_refund_transaction(
    sender: Wallet,
    *,
    htlc: str,
    nonce: int,
    params: NetworkParams = TESTNET,
) -> Transaction:
    """Build and sign a refund after the HTLC's timelock."""
    tx = Transaction(
        network=params.network_id,
        type=TxType.HTLC_REFUND,
        sender=sender.address,
        nonce=nonce,
        fee=params.min_fee,
        payload={
            "htlc_id": htlc,
            "public_key": sender.keypair.public_key_hex,
        },
    )
    return sign_transaction(sender.keypair, tx)


def claim_from_faucet(client: httpx.Client, address: str, coins: int) -> str:
    """Ask the node's faucet to pay ``address``; return the grant's txid."""
    response = client.post("/v1/faucet", json={"address": address, "amount": coins})
    if response.status_code == 429:
        raise FaucetRefused(response.text or "the faucet rate-limited this client")
    if response.status_code >= 300:
        raise TrafficError(f"POST /v1/faucet failed: HTTP {response.status_code} {response.text}")
    payload = response.json()
    digest = payload.get("txid") if isinstance(payload, Mapping) else None
    if not isinstance(digest, str) or not digest:
        raise TrafficError("POST /v1/faucet did not return a txid")
    return digest


class FaucetGateway:
    """Grant coins through the public faucet, falling back to the published key.

    The faucet budgets five grants per client per hour and one per address, so a
    cycle that seats more wallets than that cannot fund them all over HTTP. When
    the endpoint answers 429 this gateway signs the same transfer with the
    published faucet key and submits it to ``POST /v1/tx`` -- the operator path
    ``docs/operations/RUNBOOK.md`` documents. Both paths spend from the same account, so the
    gateway numbers every grant itself: it reads the committed nonce once per
    cycle, after the pool has drained, then counts its own submissions.
    """

    def __init__(
        self,
        client: httpx.Client,
        *,
        params: NetworkParams = TESTNET,
        use_faucet_endpoint: bool = True,
        wait_seconds: float = WAIT_SECONDS,
        poll_seconds: float = POLL_SECONDS,
    ) -> None:
        self._client = client
        self._params = params
        self._allow_endpoint = use_faucet_endpoint
        self._use_endpoint = use_faucet_endpoint
        self._nonce = 0
        self._wait_seconds = wait_seconds
        self._poll_seconds = poll_seconds
        self._endpoint_grants: list[str] = []
        keypair = faucet_keypair(params.network_id)
        self._faucet = Wallet(
            index=-1,
            keypair=keypair,
            address=encode_address(keypair.public_key_bytes, params),
        )

    def start_cycle(self) -> None:
        """Re-arm the endpoint and read the faucet's committed nonce.

        The read happens even when the endpoint is disabled: both paths spend from
        the same account, and a gateway that assumed nonce 0 would collide with
        its own previous cycle.
        """
        self._use_endpoint = self._allow_endpoint
        self._nonce = nonce(self._client, self._faucet.address)
        self._endpoint_grants.clear()

    def grant(self, address: str, coins: int) -> tuple[str, bool]:
        """Fund ``address``; return ``(txid, went through the faucet endpoint)``."""
        if self._use_endpoint:
            try:
                digest = claim_from_faucet(self._client, address, coins)
            except FaucetRefused as exc:
                self._use_endpoint = False
                self._settle_endpoint_grants()
                print(
                    f"traffic: {str(exc).strip()}; signing the rest of this cycle's grants "
                    "with the published faucet key",
                    file=sys.stderr,
                )
            else:
                self._endpoint_grants.append(digest)
                return digest, True
        return self._grant_with_key(address, coins), False

    def _settle_endpoint_grants(self) -> None:
        """Wait out this cycle's endpoint grants before numbering with the key.

        The faucet route numbers against the mempool projection while
        ``GET /v1/accounts`` reports the tip, so the two only agree once a block has
        mined those grants. Settling first is what keeps the key path from reusing
        a nonce the endpoint already spent -- and the wait is bounded, so a grant
        the block does not take is left behind rather than stalling the cycle:
        :meth:`_grant_with_key` re-reads the nonce and retries anyway.
        """
        if not self._endpoint_grants:
            return
        wait_for_transactions(
            self._client,
            self._endpoint_grants,
            wait_seconds=self._wait_seconds,
            poll_seconds=self._poll_seconds,
        )
        self._endpoint_grants.clear()
        self._nonce = max(self._nonce, nonce(self._client, self._faucet.address))

    def _grant_with_key(self, address: str, coins: int) -> str:
        """Sign one grant with the published faucet key, waiting out a busy nonce.

        The drip pays ``1`` coin from this same account into every empty pool, and
        the committed nonce ``GET /v1/accounts`` reports does not include a transfer
        the next block has not taken yet. A collision therefore means "the pool is
        one block behind", not "this grant is wrong": re-read the nonce, wait for
        the block, and send again until ``wait_seconds`` runs out.
        """
        deadline = time.monotonic() + self._wait_seconds
        while True:
            tx = transfer_transaction(
                self._faucet,
                to=address,
                amount=coins_to_units(coins),
                nonce=self._nonce,
                params=self._params,
            )
            try:
                digest = submit_transaction(self._client, tx)
            except NonceConflict as exc:
                if time.monotonic() >= deadline:
                    raise
                print(
                    f"traffic: {exc}; waiting for the block that frees the nonce",
                    file=sys.stderr,
                )
                time.sleep(self._poll_seconds)
                self._nonce = max(self._nonce, nonce(self._client, self._faucet.address))
                continue
            self._nonce += 1
            return digest


# -- pacing ------------------------------------------------------------------


def wait_for_transactions(
    client: httpx.Client,
    txids: Sequence[str],
    *,
    wait_seconds: float = WAIT_SECONDS,
    poll_seconds: float = POLL_SECONDS,
) -> set[str]:
    """Wait until ``txids`` leave the pool, then return the ones a block holds."""
    pending = set(txids)
    deadline = time.monotonic() + wait_seconds
    while pending and time.monotonic() < deadline:
        pending &= pending_txids(client)
        if pending:
            time.sleep(poll_seconds)
    mined: set[str] = set()
    for digest in txids:
        try:
            record = _get_object(client, f"/v1/transactions/{digest}")
        except TrafficError:
            continue
        height = record.get("height")
        if isinstance(height, int) and not isinstance(height, bool) and height >= 0:
            mined.add(digest)
    return mined


def wait_for_height(
    client: httpx.Client,
    height: int,
    *,
    wait_seconds: float = WAIT_SECONDS,
    poll_seconds: float = POLL_SECONDS,
) -> bool:
    """Wait until the chain's tip reaches ``height``; return whether it did.

    A refund is valid only once the block at its timelock exists, and the node's
    status route is the cheapest way to see that without guessing from a txid.
    """
    deadline = time.monotonic() + wait_seconds
    while True:
        if tip_height(client) >= height:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll_seconds)


@dataclass(frozen=True, slots=True)
class SideTraffic:
    """What one bridge or DEX attempt did.

    ``txids`` is everything it submitted and ``confirmed`` the subset a block has
    already taken. ``spenders`` lets the next activity avoid a nonce that is still
    pending, which is the one collision a client can see before submitting.
    """

    line: str = ""
    txids: tuple[str, ...] = ()
    confirmed: tuple[str, ...] = ()
    spenders: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CycleReport:
    """What one cycle did, for the log line and for tests."""

    cycle: int
    wallet_count: int
    funded: int
    faucet_grants: int
    operator_grants: int
    transfers: int
    confirmed: int
    unconfirmed: int = 0
    """Grants that never left the pool. Never a reason to skip the transfers."""
    side_unconfirmed: int = 0
    """Bridge or DEX transactions that never left the pool."""
    bridge: str = ""
    """The bridge leg's line, when this cycle attempted one."""
    dex: str = ""
    """The DEX action's line, when this cycle attempted one."""
    miners: str = ""
    """The churn's line, when this deployment seats extra miners."""


@dataclass(frozen=True, slots=True)
class Options:
    """The knobs ``main`` exposes, defaulted to the testnet's pacing."""

    max_wallets: int = MAX_WALLETS
    grant_coins: int = GRANT_COINS
    min_balance_coins: int = MIN_BALANCE_COINS
    max_transfer_coins: int = MAX_TRANSFER_COINS
    idle_min_seconds: float = IDLE_MIN_SECONDS
    idle_max_seconds: float = IDLE_MAX_SECONDS
    wait_seconds: float = WAIT_SECONDS
    poll_seconds: float = POLL_SECONDS
    use_faucet_endpoint: bool = True
    node: str = "http://node:8645"
    relay: str = "http://relay:8646"
    extra_miners: int = EXTRA_MINERS
    max_extra_online: int = MAX_EXTRA_ONLINE
    extra_miner_churn: bool = EXTRA_MINER_CHURN
    witness_key_files: tuple[Path, ...] = ()
    witness_urls: tuple[str, ...] = ()
    bridge: bool = True
    dex: bool = True
    bridge_coins: int = BRIDGE_COINS
    bridge_claim_blocks: int = BRIDGE_CLAIM_BLOCKS
    bridge_refund_blocks: int = BRIDGE_REFUND_BLOCKS
    dex_coins: int = DEX_COINS
    dex_asset: str | None = None
    """The variant token to pair with SCCP; ``None`` uses the network's first variant."""


DEFAULT_OPTIONS: Final = Options()
"""The testnet defaults, shared so no function takes a constructed default argument."""


@dataclass(frozen=True, slots=True)
class Extras:
    """The churn, when this deployment asks for it.

    Held apart from :class:`Options` because it is a live object with
    subprocesses behind it, not a value a caller can compare.
    """

    fleet: miners.ExtraMinerFleet | None = None
    persistent: bool = False
    """Seat the whole pool and keep it up instead of churning seats."""

    def close(self) -> None:
        """Stop the miners this process started."""
        if self.fleet is not None:
            self.fleet.close()


NO_EXTRAS: Final = Extras()
"""Wallet traffic only, shared so ``run_cycle`` needs no constructed default."""


def extras_for(
    client: httpx.Client,
    *,
    options: Options = DEFAULT_OPTIONS,
    params: NetworkParams = TESTNET,
    capacity: miners.HostCapacity | None = None,
    status_path: Path | None = None,
) -> Extras:
    """Build the churn ``options`` asks for.

    ``capacity`` is injectable so a test can decide the host's budget; when it is
    omitted the host is measured. ``status_path`` is where the fleet publishes
    what it seated; the generator passes it so a deploy can check the seats.
    """
    if options.extra_miners <= 0:
        return NO_EXTRAS
    budget = capacity
    if budget is None:
        budget = miners.host_capacity(max_extra=options.extra_miners)
    witness_files = tuple(path for path in options.witness_key_files if path.is_file())
    fleet = miners.ExtraMinerFleet(
        client,
        EXTRA_MINER_DIRECTORY,
        params=params,
        relay=options.relay,
        node=options.node,
        pool_size=options.extra_miners,
        max_online=options.max_extra_online,
        wait_seconds=options.wait_seconds,
        poll_seconds=options.poll_seconds,
        witness_key_files=witness_files,
        witness_urls=options.witness_urls,
        capacity=budget,
        status_path=status_path,
    )
    return Extras(fleet=fleet, persistent=not options.extra_miner_churn)


def _seat(pool: Sequence[Wallet], seats: int, rng: random.Random) -> list[Wallet]:
    """Pick the wallets this cycle sends from."""
    if seats >= len(pool):
        return list(pool)
    return rng.sample(list(pool), seats)


@dataclass(frozen=True, slots=True)
class Funding:
    """What one cycle's top-up did."""

    grants: tuple[str, ...] = ()
    """Grants this cycle issued, for the confirmation wait."""
    faucet: int = 0
    """How many of ``grants`` went through the HTTP faucet rather than the key."""

    @property
    def all(self) -> tuple[str, ...]:
        """Every grant this cycle issued, for the confirmation wait."""
        return self.grants


def _fund(
    client: httpx.Client,
    wallets: Sequence[Wallet],
    *,
    gateway: FaucetGateway,
    options: Options,
) -> Funding:
    """Top up every wallet that is short."""
    minimum = coins_to_units(options.min_balance_coins)
    grants: list[str] = []
    faucet = 0
    for wallet in wallets:
        if balance(client, wallet.address) >= minimum:
            continue
        digest, endpoint = gateway.grant(wallet.address, options.grant_coins)
        grants.append(digest)
        faucet += int(endpoint)
    return Funding(grants=tuple(grants), faucet=faucet)


def _send_transfers(
    client: httpx.Client,
    *,
    active: Sequence[Wallet],
    pool: Sequence[Wallet],
    rng: random.Random,
    count: int,
    options: Options,
    params: NetworkParams,
) -> list[str]:
    """Queue up to ``count`` transfers between the pool's wallets."""
    views = {item.address: account(client, item.address) for item in active}
    nonces = {
        address: _integer(view.get("nonce"), "nonce", address) for address, view in views.items()
    }
    budgets = {
        address: _integer(view.get("balance"), "balance", address)
        for address, view in views.items()
    }
    submitted: list[str] = []
    attempts = 0
    while len(submitted) < count and attempts < count * 4:
        attempts += 1
        sender = rng.choice(active)
        recipients = [item.address for item in pool if item.address != sender.address]
        if not recipients:
            break
        amount = coins_to_units(rng.randint(MIN_TRANSFER_COINS, options.max_transfer_coins))
        cost = amount + params.min_fee
        if budgets[sender.address] < cost:
            continue
        tx = transfer_transaction(
            sender,
            to=rng.choice(recipients),
            amount=amount,
            nonce=nonces[sender.address],
            params=params,
        )
        submitted.append(submit_transaction(client, tx))
        nonces[sender.address] += 1
        budgets[sender.address] -= cost
    return submitted


def _asset_label(asset: str, params: NetworkParams) -> str:
    """A pool side's ticker, falling back to a short asset id for unknown tokens."""
    if asset == dex.NATIVE_ASSET:
        return "SCCP"
    for variant in params.variants:
        if variant.asset == asset:
            return variant.symbol
    return asset[:8]


def _dex_asset_id(params: NetworkParams, override: str | None) -> str | None:
    """The variant token to pair with SCCP, or ``None`` when the network has none."""
    if override is not None:
        return override
    for variant in params.variants:
        if variant.asset:
            return variant.asset
    return None


def _short(digest: str) -> str:
    """The first 12 hex characters, enough to find a pool or HTLC in a log."""
    return digest[:12]


def _pool_side(row: Mapping[str, object], field: str) -> str:
    """One pool side from a ``GET /v1/pools`` row; an absent side is native SCCP."""
    value = row.get(field)
    if value is None or value == "":
        return dex.NATIVE_ASSET
    if not isinstance(value, str):
        raise TrafficError(f"pool {row.get('pool_id')} reported an invalid {field}: {value!r}")
    return value


def _pool_int(row: Mapping[str, object], field: str) -> int:
    return _integer(row.get(field), field, str(row.get("pool_id")))


def _pool_id(row: Mapping[str, object]) -> str:
    pool_id = row.get("pool_id")
    if not isinstance(pool_id, str) or len(pool_id) != 64:
        raise TrafficError(f"/v1/pools reported an invalid pool id: {pool_id!r}")
    return pool_id


def _pool_shares(row: Mapping[str, object]) -> Mapping[str, object]:
    shares = row.get("shares")
    if shares is None:
        return {}
    if not isinstance(shares, Mapping):
        raise TrafficError(f"pool {row.get('pool_id')} reported invalid shares: {shares!r}")
    return shares


def _max_trade_amount(view: Mapping[str, object], asset: str, fee: int) -> int:
    """How much of ``asset`` one account may spend while still paying ``fee``."""
    native = _integer(view.get("balance"), "balance", str(view.get("address")))
    if native < fee:
        return 0
    if asset == dex.NATIVE_ASSET:
        return native - fee
    return account_asset_balance(view, asset)


def _create_pool_action(
    client: httpx.Client,
    *,
    candidates: Sequence[Wallet],
    asset: str,
    options: Options,
    params: NetworkParams,
) -> SideTraffic | None:
    """Seed a native/variant pool with equal sides from the first funded wallet."""
    target = coins_to_units(options.dex_coins)
    for wallet in candidates:
        view = account(client, wallet.address)
        native = _integer(view.get("balance"), "balance", wallet.address)
        if native < params.min_fee:
            continue
        held = account_asset_balance(view, asset)
        amount = min(target, native - params.min_fee, held)
        if amount <= 0:
            continue
        pool_id = dex.pool_id(dex.NATIVE_ASSET, asset)
        tx = create_pool_transaction(
            wallet,
            asset=asset,
            amount=amount,
            nonce=_integer(view.get("nonce"), "nonce", wallet.address),
            params=params,
        )
        digest = submit_transaction(client, tx)
        label = _asset_label(asset, params)
        return SideTraffic(
            line=(
                f"dex: created SCCP/{label} pool {_short(pool_id)} ({format_coins(amount)} each)"
            ),
            txids=(digest,),
            spenders=(wallet.address,),
        )
    return None


def _swap_action(
    client: httpx.Client,
    *,
    candidates: Sequence[Wallet],
    row: Mapping[str, object],
    options: Options,
    params: NetworkParams,
) -> SideTraffic | None:
    """Swap a coin against one side of an existing pool."""
    pool_id = _pool_id(row)
    asset_a = _pool_side(row, "asset_a")
    asset_b = _pool_side(row, "asset_b")
    reserve_a = _pool_int(row, "reserve_a")
    reserve_b = _pool_int(row, "reserve_b")
    fee_bps = _pool_int(row, "fee_bps")
    if fee_bps > 1000:
        raise TrafficError(f"pool {pool_id} reported an implausible fee of {fee_bps} bp")
    target = coins_to_units(options.dex_coins)
    for wallet in candidates:
        view = account(client, wallet.address)
        if _integer(view.get("balance"), "balance", wallet.address) < params.min_fee:
            continue
        for asset_in, asset_out, reserve_in, reserve_out in (
            (asset_a, asset_b, reserve_a, reserve_b),
            (asset_b, asset_a, reserve_b, reserve_a),
        ):
            amount_in = min(target, _max_trade_amount(view, asset_in, params.min_fee))
            if amount_in <= 0:
                continue
            quote = dex.swap_output(reserve_in, reserve_out, amount_in, fee_bps)
            minimum_out = quote * (10_000 - DEX_SLIPPAGE_BPS) // 10_000
            if quote <= 0 or minimum_out <= 0:
                continue
            tx = swap_transaction(
                wallet,
                pool_id=pool_id,
                asset_in=asset_in,
                amount_in=amount_in,
                min_amount_out=minimum_out,
                nonce=_integer(view.get("nonce"), "nonce", wallet.address),
                params=params,
            )
            digest = submit_transaction(client, tx)
            return SideTraffic(
                line=(
                    f"dex: swapped {format_coins(amount_in)} {_asset_label(asset_in, params)} "
                    f"for {format_coins(quote)} {_asset_label(asset_out, params)} "
                    f"in pool {_short(pool_id)}"
                ),
                txids=(digest,),
                spenders=(wallet.address,),
            )
    return None


def _add_liquidity_action(
    client: httpx.Client,
    *,
    candidates: Sequence[Wallet],
    row: Mapping[str, object],
    options: Options,
    params: NetworkParams,
) -> SideTraffic | None:
    """Deposit a proportional one-coin-ish side into an existing pool."""
    pool_id = _pool_id(row)
    asset_a = _pool_side(row, "asset_a")
    asset_b = _pool_side(row, "asset_b")
    reserve_a = _pool_int(row, "reserve_a")
    reserve_b = _pool_int(row, "reserve_b")
    total_shares = _pool_int(row, "total_shares")
    if reserve_a <= 0 or reserve_b <= 0 or total_shares <= 0:
        return None
    target = coins_to_units(options.dex_coins)
    for wallet in candidates:
        view = account(client, wallet.address)
        max_a = _max_trade_amount(view, asset_a, params.min_fee)
        max_b = _max_trade_amount(view, asset_b, params.min_fee)
        if max_a <= 0 or max_b <= 0:
            continue
        wanted_a = min(target, max_a, max(1, reserve_a // 100))
        amount_b = dex.proportional_amount(wanted_a, reserve_a, reserve_b)
        if amount_b > max_b:
            amount_b = max_b
            amount_a = amount_b * reserve_a // reserve_b
        else:
            amount_a = wanted_a
        if amount_a <= 0 or amount_b <= 0:
            continue
        if dex.added_shares(reserve_a, reserve_b, total_shares, amount_a, amount_b) <= 0:
            continue
        tx = add_liquidity_transaction(
            wallet,
            pool_id=pool_id,
            amount_a=amount_a,
            amount_b=amount_b,
            nonce=_integer(view.get("nonce"), "nonce", wallet.address),
            params=params,
        )
        digest = submit_transaction(client, tx)
        return SideTraffic(
            line=(
                f"dex: added {format_coins(amount_a)} {_asset_label(asset_a, params)} + "
                f"{format_coins(amount_b)} {_asset_label(asset_b, params)} "
                f"to pool {_short(pool_id)}"
            ),
            txids=(digest,),
            spenders=(wallet.address,),
        )
    return None


def _remove_liquidity_action(
    client: httpx.Client,
    *,
    candidates: Sequence[Wallet],
    row: Mapping[str, object],
    params: NetworkParams,
) -> SideTraffic | None:
    """Redeem a tenth of one holder's shares, or all of them if that is too small."""
    pool_id = _pool_id(row)
    asset_a = _pool_side(row, "asset_a")
    asset_b = _pool_side(row, "asset_b")
    reserve_a = _pool_int(row, "reserve_a")
    reserve_b = _pool_int(row, "reserve_b")
    total_shares = _pool_int(row, "total_shares")
    if total_shares <= 0:
        return None
    shares_by_owner = _pool_shares(row)
    for wallet in candidates:
        owned = _integer(shares_by_owner.get(wallet.address, 0), "shares", wallet.address)
        if owned <= 0:
            continue
        view = account(client, wallet.address)
        if _integer(view.get("balance"), "balance", wallet.address) < params.min_fee:
            continue
        for shares in dict.fromkeys((max(1, owned // 10), owned)):
            amount_a, amount_b = dex.removed_amounts(reserve_a, reserve_b, total_shares, shares)
            if amount_a <= 0 or amount_b <= 0:
                continue
            tx = remove_liquidity_transaction(
                wallet,
                pool_id=pool_id,
                shares=shares,
                nonce=_integer(view.get("nonce"), "nonce", wallet.address),
                params=params,
            )
            digest = submit_transaction(client, tx)
            return SideTraffic(
                line=(
                    f"dex: removed {shares} shares "
                    f"({format_coins(amount_a)} {_asset_label(asset_a, params)} + "
                    f"{format_coins(amount_b)} {_asset_label(asset_b, params)}) "
                    f"from pool {_short(pool_id)}"
                ),
                txids=(digest,),
                spenders=(wallet.address,),
            )
    return None


def _dex_activity(
    client: httpx.Client,
    *,
    wallets: Sequence[Wallet],
    rng: random.Random,
    cycle: int,
    options: Options,
    params: NetworkParams,
    excluded: set[str],
) -> SideTraffic:
    """Create a pool if there is none, otherwise rotate a pool action.

    The rotation starts with a swap so a long-lived pool sees volume every third
    cycle, then a deposit and a withdrawal. Any action that finds no wallet with
    both sides falls through to the next, so a cycle still exercises the DEX when
    one holder is short a side.
    """
    if not options.dex:
        return SideTraffic()
    candidates = [item for item in wallets if item.address not in excluded]
    if not candidates:
        return SideTraffic("dex: skipped (every wallet has a pending spend)")
    asset = _dex_asset_id(params, options.dex_asset)
    rows = pool_views(client)
    if not rows:
        if asset is None:
            return SideTraffic("dex: skipped (the network has no variant asset to pair with SCCP)")
        created = _create_pool_action(
            client,
            candidates=candidates,
            asset=asset,
            options=options,
            params=params,
        )
        if created is not None:
            return created
        return SideTraffic(
            f"dex: skipped (no wallet held SCCP fees and {_asset_label(asset, params)})"
        )
    row = rng.choice(rows)
    offset = (cycle - 1) % len(DEX_ACTIONS)
    rotated = DEX_ACTIONS[offset:] + DEX_ACTIONS[:offset]
    for action in rotated:
        if action == "swap":
            result = _swap_action(
                client,
                candidates=candidates,
                row=row,
                options=options,
                params=params,
            )
        elif action == "add":
            result = _add_liquidity_action(
                client,
                candidates=candidates,
                row=row,
                options=options,
                params=params,
            )
        else:
            result = _remove_liquidity_action(
                client,
                candidates=candidates,
                row=row,
                params=params,
            )
        if result is not None:
            return result
    return SideTraffic(f"dex: skipped (no wallet held both sides of pool {_short(_pool_id(row))})")


def _bridge_activity(
    client: httpx.Client,
    *,
    wallets: Sequence[Wallet],
    rng: random.Random,
    cycle: int,
    options: Options,
    params: NetworkParams,
    excluded: set[str],
) -> SideTraffic:
    """Lock an HTLC and either claim it or wait out the timelock and refund it.

    Odd cycles claim, revealing the preimage; even cycles refund, which needs the
    chain to reach the timelock and is the only path that waits on a height rather
    than a txid. The lock's id is derived from its sender and nonce, exactly as a
    wallet derives it before signing.
    """
    if not options.bridge:
        return SideTraffic()
    amount = coins_to_units(options.bridge_coins)
    fee = params.min_fee
    mode = "claim" if cycle % 2 else "refund"
    candidates = [item for item in wallets if item.address not in excluded]
    if not candidates:
        return SideTraffic("bridge: skipped (every wallet has a pending spend)")
    rng.shuffle(candidates)
    pair: tuple[Wallet, Wallet] | None = None
    for sender in candidates:
        view = account(client, sender.address)
        available = _integer(view.get("balance"), "balance", sender.address)
        needs = amount + fee + (fee if mode == "refund" else 0)
        if available < needs:
            continue
        beneficiaries = [
            item
            for item in wallets
            if item.address != sender.address and item.address not in excluded
        ]
        if not beneficiaries:
            continue
        pair = (sender, rng.choice(beneficiaries))
        break
    if pair is None:
        return SideTraffic(
            f"bridge: skipped (no wallet held {format_coins(amount)} plus enough for fees)"
        )
    sender, beneficiary = pair
    tip = tip_height(client)
    lock_height = tip + 1
    if mode == "claim":
        timelock = lock_height + options.bridge_claim_blocks
    else:
        timelock = lock_height + options.bridge_refund_blocks
    preimage = rng.getrandbits(256).to_bytes(32, "big").hex()
    hashlock = hashlib.sha256(bytes.fromhex(preimage)).hexdigest()
    lock_nonce = nonce(client, sender.address)
    lock_tx = htlc_lock_transaction(
        sender,
        beneficiary=beneficiary.address,
        amount=amount,
        hashlock=hashlock,
        timelock_height=timelock,
        nonce=lock_nonce,
        params=params,
    )
    lock_digest = submit_transaction(client, lock_tx)
    htlc = htlc_id(sender.address, lock_nonce)
    lock_confirmed = wait_for_transactions(
        client,
        (lock_digest,),
        wait_seconds=options.wait_seconds,
        poll_seconds=options.poll_seconds,
    )
    if lock_digest not in lock_confirmed:
        return SideTraffic(
            line=f"bridge: locked htlc {_short(htlc)} (unconfirmed)",
            txids=(lock_digest,),
            spenders=(sender.address, beneficiary.address),
        )
    if mode == "refund" and not wait_for_height(
        client,
        timelock,
        wait_seconds=options.wait_seconds,
        poll_seconds=options.poll_seconds,
    ):
        return SideTraffic(
            line=f"bridge: locked htlc {_short(htlc)}; refund waits for height {timelock}",
            txids=(lock_digest,),
            confirmed=(lock_digest,),
            spenders=(sender.address, beneficiary.address),
        )
    if mode == "claim":
        closer = beneficiary
        close_tx = htlc_claim_transaction(
            closer,
            htlc=htlc,
            preimage=preimage,
            nonce=nonce(client, closer.address),
            params=params,
        )
    else:
        closer = sender
        close_tx = htlc_refund_transaction(
            closer,
            htlc=htlc,
            nonce=nonce(client, closer.address),
            params=params,
        )
    close_digest = submit_transaction(client, close_tx)
    close_confirmed = wait_for_transactions(
        client,
        (close_digest,),
        wait_seconds=options.wait_seconds,
        poll_seconds=options.poll_seconds,
    )
    confirmed: tuple[str, ...] = (lock_digest,)
    if close_digest in close_confirmed:
        confirmed = (lock_digest, close_digest)
    verb = "claimed" if mode == "claim" else "refunded"
    suffix = "" if close_digest in close_confirmed else " (close unconfirmed)"
    return SideTraffic(
        line=(f"bridge: locked {format_coins(amount)} SCCP and {verb} htlc {_short(htlc)}{suffix}"),
        txids=(lock_digest, close_digest),
        confirmed=confirmed,
        spenders=(sender.address, beneficiary.address),
    )


def _attempt_side(
    label: str,
    cycle: int,
    action: Callable[[], SideTraffic],
) -> SideTraffic:
    """Run one optional activity, reporting a failure without ending the cycle."""
    try:
        return action()
    except (TrafficError, httpx.HTTPError, ValueError) as exc:
        print(f"cycle {cycle}: {label} traffic skipped: {exc}", file=sys.stderr)
        return SideTraffic(f"{label}: skipped ({exc})")


def run_cycle(
    client: httpx.Client,
    *,
    cycle: int,
    pool: list[Wallet],
    rng: random.Random,
    options: Options = DEFAULT_OPTIONS,
    params: NetworkParams = TESTNET,
    wallet_count: int | None = None,
    transfer_count: int | None = None,
    extras: Extras = NO_EXTRAS,
) -> CycleReport:
    """Seat, fund, spend and confirm one cycle from a pre-seated ``pool``.

    The whole pool is funded on the first cycle, so every wallet exists and holds
    coins from the start; later cycles only top up the ones this cycle spends.

    A grant that does not confirm is reported and forgotten rather than ending the
    cycle: the wallets funded before it hold coins right now, and sending between
    them is the point of the generator. Aborting here is what once left a chain
    with five funded wallets and no circulation at all.

    ``extras`` churns miners before the grants, so a cycle's nonces are numbered
    in the order the faucet spends them.
    """
    seats = wallet_count if wallet_count is not None else rng.randint(MIN_WALLETS, MAX_WALLETS)
    seats = max(MIN_WALLETS, min(seats, options.max_wallets, len(pool)))
    active = _seat(pool, seats, rng)
    targets = list(pool) if cycle == 1 else active

    gateway = FaucetGateway(
        client,
        params=params,
        use_faucet_endpoint=options.use_faucet_endpoint,
        wait_seconds=options.wait_seconds,
        poll_seconds=options.poll_seconds,
    )
    gateway.start_cycle()
    churn = ""
    if extras.fleet is not None:
        report = (
            extras.fleet.start_all(grant=gateway.grant)
            if extras.persistent
            else extras.fleet.churn(rng, grant=gateway.grant)
        )
        churn = report.as_line()
    funding = _fund(
        client,
        targets,
        gateway=gateway,
        options=options,
    )
    waited = funding.all
    confirmed = (
        wait_for_transactions(
            client,
            waited,
            wait_seconds=options.wait_seconds,
            poll_seconds=options.poll_seconds,
        )
        if waited
        else set()
    )
    if len(confirmed) != len(waited):
        print(
            f"cycle {cycle}: {len(waited) - len(confirmed)} grant(s) never confirmed; "
            "sending with the coins the pool already holds",
            file=sys.stderr,
        )

    sends = (
        transfer_count if transfer_count is not None else rng.randint(MIN_TRANSFERS, MAX_TRANSFERS)
    )
    transfer_txids = _send_transfers(
        client,
        active=active,
        pool=pool,
        rng=rng,
        count=sends,
        options=options,
        params=params,
    )
    mined = (
        wait_for_transactions(
            client,
            transfer_txids,
            wait_seconds=options.wait_seconds,
            poll_seconds=options.poll_seconds,
        )
        if transfer_txids
        else set()
    )
    grant_txids = set(funding.all)
    grants_confirmed = sum(1 for txid in confirmed if txid in grant_txids)
    excluded = {item.address for item in active} if set(transfer_txids) - mined else set()
    bridge = _attempt_side(
        "bridge",
        cycle,
        lambda: _bridge_activity(
            client,
            wallets=pool,
            rng=rng,
            cycle=cycle,
            options=options,
            params=params,
            excluded=excluded,
        ),
    )
    dex = _attempt_side(
        "dex",
        cycle,
        lambda: _dex_activity(
            client,
            wallets=pool,
            rng=rng,
            cycle=cycle,
            options=options,
            params=params,
            excluded=excluded,
        ),
    )
    side_txids = bridge.txids + dex.txids
    # The bridge already waited for its own lock and close; only the DEX action
    # still needs the cycle to see it mined.
    settled = (
        wait_for_transactions(
            client,
            dex.txids,
            wait_seconds=options.wait_seconds,
            poll_seconds=options.poll_seconds,
        )
        if dex.txids
        else set()
    )
    side_confirmed = set(bridge.confirmed) | settled
    return CycleReport(
        cycle=cycle,
        wallet_count=len(active),
        funded=len(funding.grants),
        faucet_grants=funding.faucet,
        operator_grants=len(funding.grants) - funding.faucet,
        transfers=len(transfer_txids),
        confirmed=len(confirmed) + len(mined) + len(side_confirmed),
        unconfirmed=len(funding.all) - grants_confirmed,
        side_unconfirmed=len(side_txids) - len(side_confirmed),
        bridge=bridge.line,
        dex=dex.line,
        miners=churn,
    )


def _summary(report: CycleReport) -> str:
    """One greppable line per cycle."""
    line = (
        f"cycle {report.cycle}: {report.wallet_count} wallets, funded {report.funded} "
        f"({report.faucet_grants} faucet, {report.operator_grants} key), "
        f"{report.transfers} transfers, {report.confirmed} confirmed"
    )
    if report.unconfirmed:
        line += f", {report.unconfirmed} grant(s) unconfirmed"
    if report.side_unconfirmed:
        line += f", {report.side_unconfirmed} side tx unconfirmed"
    if report.bridge:
        line += f" | {report.bridge}"
    if report.dex:
        line += f" | {report.dex}"
    if report.miners:
        line += f" | {report.miners}"
    return line


def run(
    client: httpx.Client,
    *,
    options: Options = DEFAULT_OPTIONS,
    params: NetworkParams = TESTNET,
    rng: random.Random | None = None,
    cycles: int | None = None,
    sleep: Callable[[float], None] = time.sleep,
    extras: Extras | None = None,
) -> list[CycleReport]:
    """Run cycles until interrupted (or ``cycles`` times) and return the reports.

    ``extras`` is built from ``options`` when the caller omits it, and always
    closed on the way out: a churn that outlived the generator would leave miners
    playing for a container that no longer reports on them.
    """
    generator = random.Random() if rng is None else rng
    pool = [wallet(index, params) for index in range(options.max_wallets)]
    print(
        f"synthetic traffic on {params.network_id}: {len(pool)} wallets, "
        f"{MIN_WALLETS}-{len(pool)} active per cycle, {MIN_TRANSFERS}-{MAX_TRANSFERS} sends "
        f"of up to {format_coins(coins_to_units(options.max_transfer_coins))} coins each",
        flush=True,
    )
    if extras is None:
        extras = extras_for(client, options=options, params=params, status_path=FLEET_STATUS_PATH)
    if extras.fleet is not None:
        # Announce before the first cycle: `/stack/synthetic-fleet.json` is what a
        # deploy reads to tell a seated fleet from a missing one, and the first
        # cycle only reaches `start_all` after it has started funding wallets.
        extras.fleet.announce()
    reports: list[CycleReport] = []
    completed = 0
    try:
        while cycles is None or completed < cycles:
            completed += 1
            try:
                report = run_cycle(
                    client,
                    cycle=completed,
                    pool=pool,
                    rng=generator,
                    options=options,
                    params=params,
                    extras=extras,
                )
            except (httpx.HTTPError, TrafficError, ValueError) as exc:
                print(f"cycle {completed}: {exc}", file=sys.stderr, flush=True)
            else:
                reports.append(report)
                print(_summary(report), flush=True)
            if cycles is not None and completed >= cycles:
                break
            sleep(generator.uniform(options.idle_min_seconds, options.idle_max_seconds))
    finally:
        extras.close()
    return reports


def _parser() -> argparse.ArgumentParser:
    """The command line the compose service runs."""
    parser = argparse.ArgumentParser(
        description="Keep a running testnet busy with wallet, bridge, and DEX traffic."
    )
    parser.add_argument(
        "--node", default="http://node:8645", help="node base URL (default: %(default)s)"
    )
    parser.add_argument(
        "--max-wallets",
        type=int,
        default=MAX_WALLETS,
        help=f"wallet pool cap (default: %(default)s, at least {MIN_WALLETS})",
    )
    parser.add_argument(
        "--grant-coins",
        type=int,
        default=GRANT_COINS,
        help="coins per faucet grant (default: %(default)s)",
    )
    parser.add_argument(
        "--min-balance-coins",
        type=int,
        default=MIN_BALANCE_COINS,
        help="top a wallet up when it holds less than this (default: %(default)s)",
    )
    parser.add_argument(
        "--max-transfer-coins",
        type=int,
        default=MAX_TRANSFER_COINS,
        help="largest single send, in coins (default: %(default)s)",
    )
    parser.add_argument(
        "--idle-min-seconds",
        type=float,
        default=IDLE_MIN_SECONDS,
        help="shortest pause between cycles (default: %(default)s)",
    )
    parser.add_argument(
        "--idle-max-seconds",
        type=float,
        default=IDLE_MAX_SECONDS,
        help="longest pause between cycles (default: %(default)s)",
    )
    parser.add_argument(
        "--wait-seconds",
        type=float,
        default=WAIT_SECONDS,
        help="how long to wait for a block (default: %(default)s)",
    )
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=POLL_SECONDS,
        help="pool and status poll interval (default: %(default)s)",
    )
    parser.add_argument(
        "--no-faucet-endpoint",
        dest="use_faucet_endpoint",
        action="store_false",
        help="always sign grants with the published faucet key",
    )
    parser.add_argument(
        "--relay",
        default="http://relay:8646",
        help="relay base URL the extra miners play against (default: %(default)s)",
    )
    parser.add_argument(
        "--extra-miners",
        type=int,
        default=EXTRA_MINERS,
        help="extra miners in the churn pool; 0 disables it (default: %(default)s)",
    )
    parser.add_argument(
        "--max-extra-online",
        type=int,
        default=MAX_EXTRA_ONLINE,
        help="how many extra miners may run at once (default: %(default)s)",
    )
    parser.add_argument(
        "--extra-miner-churn",
        dest="extra_miner_churn",
        action="store_true",
        default=EXTRA_MINER_CHURN,
        help="swap pool members in and out instead of seating them permanently",
    )
    parser.add_argument(
        "--no-extra-miner-churn",
        dest="extra_miner_churn",
        action="store_false",
        help="keep every pool member seated (the testnet's fixed roles)",
    )
    parser.add_argument(
        "--witness-key-file",
        type=Path,
        action="append",
        default=None,
        help=(
            "clock witness public key file the extra miners trust "
            "(repeatable; default: the stack's three)"
        ),
    )
    parser.add_argument(
        "--witness-url",
        action="append",
        default=None,
        help="independent witness base URL the extra miners may ask for evidence",
    )
    parser.add_argument(
        "--no-bridge",
        dest="bridge",
        action="store_false",
        help="skip the HTLC lock/claim/refund traffic",
    )
    parser.add_argument(
        "--no-dex",
        dest="dex",
        action="store_false",
        help="skip the pool and swap traffic",
    )
    parser.add_argument(
        "--bridge-coins",
        type=int,
        default=BRIDGE_COINS,
        help="coins in each HTLC (default: %(default)s)",
    )
    parser.add_argument(
        "--dex-coins",
        type=int,
        default=DEX_COINS,
        help="target size of each DEX side, in coins (default: %(default)s)",
    )
    parser.add_argument(
        "--dex-asset",
        default=None,
        help="variant asset id to pair with SCCP (default: the network's first variant)",
    )
    parser.add_argument(
        "--cycles",
        type=int,
        default=None,
        help="stop after this many cycles (default: run until interrupted)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Parse the command line and run the generator."""
    parser = _parser()
    args = parser.parse_args(argv)
    if args.max_wallets < MIN_WALLETS:
        parser.error(f"--max-wallets must be at least {MIN_WALLETS}")
    if args.grant_coins < 1:
        parser.error("--grant-coins must be at least 1")
    if args.min_balance_coins < 1:
        parser.error("--min-balance-coins must be at least 1")
    if args.max_transfer_coins < 1:
        parser.error("--max-transfer-coins must be at least 1")
    if args.idle_max_seconds < args.idle_min_seconds:
        parser.error("--idle-max-seconds must not be below --idle-min-seconds")
    if args.wait_seconds <= 0:
        parser.error("--wait-seconds must be positive")
    if args.poll_seconds <= 0:
        parser.error("--poll-seconds must be positive")
    if args.extra_miners < 0:
        parser.error("--extra-miners cannot be negative")
    if args.max_extra_online < 1:
        parser.error("--max-extra-online must be at least 1")
    if args.bridge_coins < 1:
        parser.error("--bridge-coins must be at least 1")
    if args.dex_coins < 1:
        parser.error("--dex-coins must be at least 1")
    if args.dex_asset is not None and (
        len(args.dex_asset) != 64
        or args.dex_asset != args.dex_asset.lower()
        or not all(character in "0123456789abcdef" for character in args.dex_asset)
    ):
        parser.error("--dex-asset must be a 64-character lowercase hex asset id")
    if args.cycles is not None and args.cycles < 1:
        parser.error("--cycles must be at least 1")
    options = Options(
        max_wallets=args.max_wallets,
        grant_coins=args.grant_coins,
        min_balance_coins=args.min_balance_coins,
        max_transfer_coins=args.max_transfer_coins,
        idle_min_seconds=args.idle_min_seconds,
        idle_max_seconds=args.idle_max_seconds,
        wait_seconds=args.wait_seconds,
        poll_seconds=args.poll_seconds,
        use_faucet_endpoint=args.use_faucet_endpoint,
        node=args.node,
        relay=args.relay,
        extra_miners=args.extra_miners,
        max_extra_online=args.max_extra_online,
        extra_miner_churn=args.extra_miner_churn,
        witness_key_files=(
            EXTRA_WITNESS_KEY_FILES
            if args.witness_key_file is None
            else tuple(args.witness_key_file)
        ),
        witness_urls=tuple(args.witness_url or ()),
        bridge=args.bridge,
        dex=args.dex,
        bridge_coins=args.bridge_coins,
        dex_coins=args.dex_coins,
        dex_asset=args.dex_asset,
    )
    try:
        with httpx.Client(base_url=args.node, timeout=30.0) as client:
            run(client, options=options, cycles=args.cycles)
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
