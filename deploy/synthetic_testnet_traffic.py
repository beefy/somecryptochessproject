"""Drive realistic wallet traffic through a running testnet node.

``drip_testnet_traffic`` keeps an epoch supplied with the one piece of ledger work
every block needs, but it only acts on an *empty* pool with a single faucet
transfer: it keeps the chain moving, it does not exercise what a user does. This
script does. It creates its whole pool on startup, then every cycle it

1. picks 2-10 wallets from that pool (a random count),
2. funds each one from the faucet, topping up only those that are short,
3. waits for those grants to be mined,
4. sends 1-10 transfers between the wallets,
5. waits for those transfers to be mined, then
6. idles for 1-5 minutes before starting over.

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
  account must be numbered by the client -- the same rule ``docs/RUNBOOK.md``
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
faucet key, the operator path ``docs/RUNBOOK.md`` documents for recovering a test
wallet. The fallback is logged, and the endpoint is tried again next cycle, so
the faucet itself stays in the loop whenever it has budget.

Two further kinds of activity ride along, because a deployed chain that only ever
sees well-behaved wallets never exercises them:

* ``--extra-miners`` seats that many ``sccp miner run`` children from fixed seeds
  (``synthetic_miner_creation``) and churns one per cycle, so registrations,
  availability commitments and roster changes happen on a chain nobody is
  watching. The host's CPU and memory decide how many may actually run; a small
  VM simply gets none.
* ``--quality-events`` re-runs the pinned quality profile over a proof from a
  recent block and replays the challenge it would warrant
  (``synthetic_quality_events``), so the path from "a proof was accepted" to "a
  bond moved" prints a line every cycle even though testnet enforces no quality.

Both spend from the same faucet as the wallets, through the same gateway, so all
of a cycle's nonces are numbered in one place. Both are one flag away from off:
``--extra-miners 0 --no-quality-events`` leaves exactly the wallet traffic.

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

from sccp.chain.genesis import faucet_keypair
from sccp.chain.tx import sign_transaction, txid
from sccp.config import TESTNET, NetworkParams, coins_to_units, format_coins
from sccp.crypto.address import encode_address
from sccp.crypto.pq import HybridKeypair, hybrid_keypair_from_seed
from sccp.protocol.models import Transaction, TxType

try:  # the tests import this module as ``deploy.synthetic_testnet_traffic``
    from deploy import synthetic_miner_creation as miners
    from deploy import synthetic_quality_events as quality
except ImportError:  # pragma: no cover - compose runs the file, not the package
    import synthetic_miner_creation as miners  # type: ignore[no-redef,import-not-found]
    import synthetic_quality_events as quality  # type: ignore[no-redef,import-not-found]

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
"""How many extra miners the pool holds. ``0`` seats none and disables the churn."""

MAX_EXTRA_ONLINE: Final = 2
"""How many of that pool may run at once, so the churn has room to move either way."""

EXTRA_MINER_DIRECTORY: Final = Path("/data/synthetic-miners")
"""Where extra miners keep keyfiles. Inside the container on purpose: every
identity comes from a published seed, so nothing here has to outlive a redeploy."""

QUALITY_EVENTS: Final = True
"""Re-run one proof per cycle and replay the challenge it would warrant."""


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
    ``docs/RUNBOOK.md`` documents. Both paths spend from the same account, so the
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
    miners: str = ""
    """The churn's line, when this deployment seats extra miners."""
    quality: str = ""
    """The audit's line, when this deployment re-runs quality profiles."""


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
    quality_events: bool = QUALITY_EVENTS


DEFAULT_OPTIONS: Final = Options()
"""The testnet defaults, shared so no function takes a constructed default argument."""


@dataclass(frozen=True, slots=True)
class Extras:
    """The churn and the audit, when this deployment asks for them.

    Held apart from :class:`Options` because they are live objects with
    subprocesses and an engine behind them, not values a caller can compare.
    """

    fleet: miners.ExtraMinerFleet | None = None
    audit: quality.QualityAudit | None = None

    def close(self) -> None:
        """Stop the miners and the engine this process started."""
        if self.fleet is not None:
            self.fleet.close()
        if self.audit is not None:
            self.audit.close()


NO_EXTRAS: Final = Extras()
"""Wallet traffic only, shared so ``run_cycle`` needs no constructed default."""


def extras_for(
    client: httpx.Client,
    *,
    options: Options = DEFAULT_OPTIONS,
    params: NetworkParams = TESTNET,
    capacity: miners.HostCapacity | None = None,
) -> Extras:
    """Build the churn and the audit ``options`` asks for.

    ``capacity`` is injectable so a test can decide the host's budget; when it is
    omitted the host is measured, and the audit's own engine is reserved as one
    more miner because it competes for the same cores.
    """
    fleet: miners.ExtraMinerFleet | None = None
    if options.extra_miners > 0:
        budget = capacity
        if budget is None:
            budget = miners.host_capacity(
                fixed_miners=miners.FIXED_MINERS + int(options.quality_events),
                max_extra=options.extra_miners,
            )
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
            capacity=budget,
        )
    audit = quality.QualityAudit(client, params=params) if options.quality_events else None
    return Extras(fleet=fleet, audit=audit)


def _seat(pool: Sequence[Wallet], seats: int, rng: random.Random) -> list[Wallet]:
    """Pick the wallets this cycle sends from."""
    if seats >= len(pool):
        return list(pool)
    return rng.sample(list(pool), seats)


def _fund(
    client: httpx.Client,
    wallets: Sequence[Wallet],
    *,
    gateway: FaucetGateway,
    options: Options,
    params: NetworkParams,
) -> tuple[list[str], int]:
    """Top up every wallet that is short; return its txids and the HTTP count."""
    minimum = coins_to_units(options.min_balance_coins)
    grants: list[str] = []
    via_endpoint = 0
    for item in wallets:
        if balance(client, item.address) >= minimum:
            continue
        digest, endpoint = gateway.grant(item.address, options.grant_coins)
        grants.append(digest)
        via_endpoint += int(endpoint)
    return grants, via_endpoint


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

    ``extras`` churns miners before the grants and audits quality after the
    transfers, so a cycle's nonces are numbered in the order the faucet spends
    them and the audit sees a block that is already settled.
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
        churn = extras.fleet.churn(rng, grant=gateway.grant).as_line()
    grants, faucet_grants = _fund(client, targets, gateway=gateway, options=options, params=params)
    confirmed = (
        wait_for_transactions(
            client,
            grants,
            wait_seconds=options.wait_seconds,
            poll_seconds=options.poll_seconds,
        )
        if grants
        else set()
    )
    if len(confirmed) != len(grants):
        print(
            f"cycle {cycle}: {len(grants) - len(confirmed)} grant(s) never confirmed; "
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
    audited = ""
    if extras.audit is not None:
        try:
            audited = extras.audit.audit().as_line()
        except (quality.QualityEventError, httpx.HTTPError) as exc:
            audited = f"quality: {exc}"
    return CycleReport(
        cycle=cycle,
        wallet_count=len(active),
        funded=len(grants),
        faucet_grants=faucet_grants,
        operator_grants=len(grants) - faucet_grants,
        transfers=len(transfer_txids),
        confirmed=len(confirmed) + len(mined),
        unconfirmed=len(grants) - len(confirmed),
        miners=churn,
        quality=audited,
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
    for extra in (report.miners, report.quality):
        if extra:
            line += f" | {extra}"
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
        extras = extras_for(client, options=options, params=params)
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
        description="Keep a running testnet busy with ordinary wallet traffic."
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
        "--no-quality-events",
        dest="quality_events",
        action="store_false",
        help="skip the per-cycle quality re-run and its replayed challenge",
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
        quality_events=args.quality_events,
    )
    try:
        with httpx.Client(base_url=args.node, timeout=30.0) as client:
            run(client, options=options, cycles=args.cycles)
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
