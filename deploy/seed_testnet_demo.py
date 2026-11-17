"""Verify (or create) the testnet demo bridge leg and SCCP/C960 pool.

Testnet genesis now seeds both records at height 0: the pool is backed by 5 SCCP
and 5 C960 allocations and the HTLC was locked and claimed with the published
demo preimage. On a normal deployment this helper therefore finds the existing
records and reports them without sending a transaction.

It remains a fallback for a custom chain whose genesis does not seed them. In
that case it:

1. funds two deterministic demo wallets from the published testnet faucet;
2. locks one SCCP in an HTLC and claims it, leaving a completed bridge leg in
   ``GET /v1/htlcs``;
3. waits for the first mature C960 coinbase on one of the stack's Chess960
   miners, transfers enough C960 to the demo liquidity wallet, and creates the
   SCCP/C960 pool with equal sides.

Every step is an ordinary signed transaction submitted through ``POST /v1/tx``
and validated by the node. Only the key sources are special: the two demo
wallets come from a published seed, and the C960 transfer uses the stack's
published testnet miner keyfiles. The Ethereum leg of the bridge is still an
operator concern: deploying ``EthHtlc`` on Sepolia needs a funded deployer key,
so this script creates the SCCP leg only and leaves the node's bridge
environment to ``SCCP_ETH_RPC_URL`` / ``SCCP_ETH_HTLC_CONTRACT``.

Usage::

    python -m deploy.seed_testnet_demo --stack /stack --node http://node:8645
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import httpx

from deploy import synthetic_testnet_traffic as traffic
from deploy.bootstrap_testnet_stack import (
    BOOTSTRAP_HEIGHT,
    BootstrapError,
    BootstrapMiner,
    load_stack,
)
from sccp.chain import dex
from sccp.chain.genesis import (
    testnet_bridge_hashlock,
    testnet_bridge_preimage,
    testnet_demo_keypair,
)
from sccp.chain.tx import htlc_id, sign_transaction
from sccp.config import (
    CHESS960_ASSET_ID,
    TESTNET,
    NetworkParams,
    coins_to_units,
    format_coins,
)
from sccp.crypto.address import encode_address
from sccp.protocol.models import Transaction, TxType

GRANT_COINS: Final = 20
"""SCCP granted to each demo wallet; enough for the bridge and the pool deposit."""

DEX_COINS: Final = 5
"""Size of each side of the SCCP/C960 pool, in whole coins."""

BRIDGE_COINS: Final = 1
"""Native SCCP locked in the demo HTLC."""

BRIDGE_CLAIM_BLOCKS: Final = 12
"""Blocks between the lock and its timelock; the claim lands well inside it."""

WAIT_SECONDS: Final = 1_800.0
"""How long to wait for a mature C960 coinbase: 30 blocks at one minute each."""

POLL_SECONDS: Final = 10.0
"""How often to re-read the miner balances while waiting for C960."""


class SeedError(RuntimeError):
    """The demo pool or bridge leg could not be created."""


@dataclass(frozen=True, slots=True)
class SeedOptions:
    """The knobs ``main`` exposes, defaulted to the compose deployment."""

    grant_coins: int = GRANT_COINS
    dex_coins: int = DEX_COINS
    bridge_coins: int = BRIDGE_COINS
    bridge_claim_blocks: int = BRIDGE_CLAIM_BLOCKS
    wait_seconds: float = WAIT_SECONDS
    poll_seconds: float = POLL_SECONDS
    use_faucet_endpoint: bool = True


@dataclass(frozen=True, slots=True)
class SeedReport:
    """What one seeding pass did, for the log line and for tests."""

    bridge: str
    dex: str


def demo_wallet(index: int, params: NetworkParams = TESTNET) -> traffic.Wallet:
    """Derive one deterministic demo wallet from the published seed namespace."""
    if index < 0:
        raise ValueError("a demo wallet index must be non-negative")
    keypair = testnet_demo_keypair(index)
    return traffic.Wallet(
        index=-1 - index,
        keypair=keypair,
        address=encode_address(keypair.public_key_bytes, params),
    )


def variant_transfer_transaction(
    sender: traffic.Wallet,
    *,
    to: str,
    asset: str,
    amount: int,
    nonce: int,
    params: NetworkParams = TESTNET,
) -> Transaction:
    """Build and sign a transfer of a variant token; the fee is still SCCP."""
    tx = Transaction(
        network=params.network_id,
        type=TxType.TRANSFER,
        sender=sender.address,
        nonce=nonce,
        fee=params.min_fee,
        payload={
            "to": to,
            "amount": amount,
            "asset": asset,
            "public_key": sender.keypair.public_key_hex,
        },
    )
    return sign_transaction(sender.keypair, tx)


def _get_object(
    client: httpx.Client,
    path: str,
    *,
    params: Mapping[str, str] | None = None,
) -> Mapping[str, object]:
    """GET ``path`` and return a JSON object, or raise :class:`SeedError`."""
    try:
        response = client.get(path, params=params)
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise SeedError(f"GET {path} failed: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise SeedError(f"GET {path} did not return an object")
    return payload


def _height(client: httpx.Client) -> int:
    """The node's current height, or a :class:`SeedError` if it is unreadable."""
    value = _get_object(client, "/v1/status").get("height")
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise SeedError(f"/v1/status reported an invalid height: {value!r}")
    return value


def wait_for_bootstrap(client: httpx.Client, *, wait_seconds: float) -> int:
    """Wait until the node has adopted the bootstrap chain."""
    deadline = time.monotonic() + wait_seconds
    last_error = "no response"
    while True:
        try:
            height = _height(client)
        except SeedError as exc:
            last_error = str(exc)
        else:
            if height >= BOOTSTRAP_HEIGHT:
                return height
            last_error = f"node is at height {height}, expected {BOOTSTRAP_HEIGHT}"
        if time.monotonic() >= deadline:
            raise SeedError(f"the node never reached the bootstrap height: {last_error}")
        time.sleep(POLL_SECONDS)


def _account(client: httpx.Client, address: str) -> Mapping[str, object]:
    """The committed account view for ``address``."""
    return _get_object(client, f"/v1/accounts/{address}")


def _native_balance(client: httpx.Client, address: str) -> int:
    value = _account(client, address).get("balance")
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise SeedError(f"{address} reported an invalid balance: {value!r}")
    return value


def _submit(
    client: httpx.Client,
    build: Callable[[int], Transaction],
    *,
    spender: str,
    wait_seconds: float,
    poll_seconds: float,
) -> tuple[str, Transaction]:
    """Submit ``build(nonce)``, retrying a nonce another client already spent.

    The drip, the miner process and the synthetic traffic generator all share
    accounts with this seeder on purpose. A committed nonce read can be stale
    while a transfer is still in the pool, so a refusal that names that nonce is
    retried after the block that frees it.
    """
    deadline = time.monotonic() + wait_seconds
    while True:
        nonce = traffic.nonce(client, spender)
        tx = build(nonce)
        try:
            return traffic.submit_transaction(client, tx), tx
        except traffic.NonceConflict:
            if time.monotonic() >= deadline:
                raise SeedError(
                    f"{spender} kept a pending transaction at nonce {nonce}; "
                    "the seeder could not number its transaction"
                ) from None
            time.sleep(max(poll_seconds, 0.01))


def _wait(client: httpx.Client, txids: Sequence[str], options: SeedOptions) -> None:
    """Wait for ``txids`` to be mined, or raise :class:`SeedError`."""
    confirmed = traffic.wait_for_transactions(
        client,
        txids,
        wait_seconds=options.wait_seconds,
        poll_seconds=options.poll_seconds,
    )
    missing = set(txids) - confirmed
    if missing:
        raise SeedError(f"transactions never confirmed: {', '.join(sorted(missing))}")


def _fund_demo_wallets(
    client: httpx.Client,
    wallets: Sequence[traffic.Wallet],
    options: SeedOptions,
    params: NetworkParams = TESTNET,
) -> None:
    """Top up every demo wallet through the faucet, falling back to its key."""
    # Fund for the work, not for the full grant: a restart after a successful
    # run still holds most of the first grant, and re-granting every time would
    # hit the faucet's one-grant-per-address cooldown forever.
    needed = coins_to_units(options.bridge_coins + options.dex_coins) + 3 * params.min_fee
    short = [wallet for wallet in wallets if _native_balance(client, wallet.address) < needed]
    if not short:
        return
    gateway = traffic.FaucetGateway(
        client,
        use_faucet_endpoint=options.use_faucet_endpoint,
        wait_seconds=options.wait_seconds,
        poll_seconds=options.poll_seconds,
    )
    gateway.start_cycle()
    grants: list[str] = []
    for wallet in short:
        digest, _endpoint = gateway.grant(wallet.address, options.grant_coins)
        grants.append(digest)
    _wait(client, grants, options)


def _has_bridge_lock(client: httpx.Client, address: str) -> bool:
    """Whether ``address`` already has an HTLC, so a restart does not duplicate it."""
    payload = _get_object(client, "/v1/htlcs", params={"address": address})
    htlcs = payload.get("htlcs")
    if htlcs is None:
        return False
    if not isinstance(htlcs, list):
        raise SeedError(f"/v1/htlcs reported invalid locks: {htlcs!r}")
    return bool(htlcs)


def seed_bridge(
    client: httpx.Client,
    *,
    sender: traffic.Wallet,
    beneficiary: traffic.Wallet,
    options: SeedOptions,
    params: NetworkParams = TESTNET,
) -> str:
    """Create and claim one SCCP HTLC, leaving a completed bridge leg on chain."""
    if _has_bridge_lock(client, sender.address):
        return "bridge: demo leg already on chain"
    tip = _height(client)
    timelock = tip + 1 + options.bridge_claim_blocks
    preimage = testnet_bridge_preimage()
    hashlock = testnet_bridge_hashlock()
    amount = coins_to_units(options.bridge_coins)

    lock_digest, lock_tx = _submit(
        client,
        lambda nonce: traffic.htlc_lock_transaction(
            sender,
            beneficiary=beneficiary.address,
            amount=amount,
            hashlock=hashlock,
            timelock_height=timelock,
            nonce=nonce,
            params=params,
        ),
        spender=sender.address,
        wait_seconds=options.wait_seconds,
        poll_seconds=options.poll_seconds,
    )
    _wait(client, (lock_digest,), options)

    claim_digest, _claim_tx = _submit(
        client,
        lambda nonce: traffic.htlc_claim_transaction(
            beneficiary,
            htlc=htlc_id(sender.address, lock_tx.nonce),
            preimage=preimage,
            nonce=nonce,
            params=params,
        ),
        spender=beneficiary.address,
        wait_seconds=options.wait_seconds,
        poll_seconds=options.poll_seconds,
    )
    _wait(client, (claim_digest,), options)
    short = htlc_id(sender.address, lock_tx.nonce)[:12]
    return f"bridge: locked {options.bridge_coins} SCCP and claimed htlc {short}"


def _pool_exists(client: httpx.Client, pool_id: str) -> bool:
    """Whether the node already serves the SCCP/C960 pool."""
    payload = _get_object(client, "/v1/pools")
    pools = payload.get("pools")
    if pools is None:
        return False
    if not isinstance(pools, list):
        raise SeedError(f"/v1/pools reported invalid pools: {pools!r}")
    return any(isinstance(row, Mapping) and row.get("pool_id") == pool_id for row in pools)


def _chess960_balance(view: Mapping[str, object]) -> int:
    """A miner's spendable C960, read from an account view."""
    assets = view.get("assets")
    if not isinstance(assets, Mapping):
        return 0
    value = assets.get(CHESS960_ASSET_ID, 0)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise SeedError(f"an account reported invalid C960: {value!r}")
    return value


def _wait_for_chess960(
    client: httpx.Client,
    miners: Sequence[BootstrapMiner],
    *,
    needed: int,
    options: SeedOptions,
    params: NetworkParams = TESTNET,
) -> tuple[BootstrapMiner, int]:
    """Wait for one Chess960 miner to hold enough mature C960 to seed the pool."""
    deadline = time.monotonic() + options.wait_seconds
    while True:
        waiting: list[str] = []
        for miner in miners:
            if miner.variant != "chess960":
                continue
            view = _account(client, miner.address)
            available = _chess960_balance(view)
            if available >= needed and _native_balance(client, miner.address) >= params.min_fee:
                return miner, available
            immature = view.get("immature_assets")
            pending = 0
            if isinstance(immature, Mapping):
                value = immature.get(CHESS960_ASSET_ID, 0)
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    pending = value
            waiting.append(
                f"{miner.name}: {format_coins(available)} C960 spendable + "
                f"{format_coins(pending)} C960 immature"
            )
        if time.monotonic() >= deadline:
            detail = "; ".join(waiting) or "the stack has no Chess960 miners"
            raise SeedError(f"no mature C960 arrived within {options.wait_seconds:g}s ({detail})")
        if waiting:
            print(f"seed: waiting for C960 ({'; '.join(waiting)})", flush=True)
        time.sleep(max(options.poll_seconds, 0.01))


def seed_dex(
    client: httpx.Client,
    *,
    miners: Sequence[BootstrapMiner],
    liquidity: traffic.Wallet,
    options: SeedOptions,
    params: NetworkParams = TESTNET,
) -> str:
    """Create the SCCP/C960 pool, funding it from the first mature C960 coinbase."""
    pool_id = dex.pool_id(dex.NATIVE_ASSET, CHESS960_ASSET_ID)
    if _pool_exists(client, pool_id):
        return f"dex: SCCP/C960 pool {pool_id[:12]} already exists"

    target = coins_to_units(options.dex_coins)
    view = _account(client, liquidity.address)
    held = _chess960_balance(view)
    if held < target:
        miner, available = _wait_for_chess960(
            client,
            miners,
            needed=target - held,
            options=options,
            params=params,
        )
        amount = min(target - held, available)
        digest, transfer = _submit(
            client,
            lambda nonce: variant_transfer_transaction(
                traffic.Wallet(index=-1, keypair=miner.keypair, address=miner.address),
                to=liquidity.address,
                asset=CHESS960_ASSET_ID,
                amount=amount,
                nonce=nonce,
                params=params,
            ),
            spender=miner.address,
            wait_seconds=options.wait_seconds,
            poll_seconds=options.poll_seconds,
        )
        _wait(client, (digest,), options)
        print(
            f"seed: moved {format_coins(amount)} C960 from {miner.name} "
            f"to the liquidity wallet (nonce {transfer.nonce})",
            flush=True,
        )

    view = _account(client, liquidity.address)
    available = min(target, _chess960_balance(view))
    native = _native_balance(client, liquidity.address)
    if available <= 0:
        raise SeedError("the liquidity wallet holds no C960 after the transfer")
    amount = min(available, native - params.min_fee)
    if amount <= 0:
        raise SeedError("the liquidity wallet cannot pay the pool deposit and its fee")
    digest, _tx = _submit(
        client,
        lambda nonce: traffic.create_pool_transaction(
            liquidity,
            asset=CHESS960_ASSET_ID,
            amount=amount,
            nonce=nonce,
            params=params,
        ),
        spender=liquidity.address,
        wait_seconds=options.wait_seconds,
        poll_seconds=options.poll_seconds,
    )
    _wait(client, (digest,), options)
    return f"dex: created SCCP/C960 pool {pool_id[:12]} with {format_coins(amount)} each"


def seed_once(
    client: httpx.Client,
    *,
    miners: Sequence[BootstrapMiner],
    options: SeedOptions | None = None,
    params: NetworkParams = TESTNET,
) -> SeedReport:
    """Fund the demo wallets, create the bridge leg, then create the DEX pool."""
    options = SeedOptions() if options is None else options
    liquidity = demo_wallet(0, params)
    beneficiary = demo_wallet(1, params)
    pool_id = dex.pool_id(dex.NATIVE_ASSET, CHESS960_ASSET_ID)
    if _has_bridge_lock(client, liquidity.address) and _pool_exists(client, pool_id):
        return SeedReport(
            bridge="bridge: demo leg already on chain",
            dex=f"dex: SCCP/C960 pool {pool_id[:12]} already exists",
        )
    _fund_demo_wallets(client, (liquidity, beneficiary), options, params)
    bridge = seed_bridge(
        client,
        sender=liquidity,
        beneficiary=beneficiary,
        options=options,
        params=params,
    )
    dex_line = seed_dex(
        client,
        miners=miners,
        liquidity=liquidity,
        options=options,
        params=params,
    )
    return SeedReport(bridge=bridge, dex=dex_line)


def _parser() -> argparse.ArgumentParser:
    """The command line the compose service runs."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node", default="http://node:8645", help="node base URL")
    parser.add_argument("--stack", type=Path, default=Path("/stack"), help="stack directory")
    parser.add_argument("--grant-coins", type=int, default=GRANT_COINS)
    parser.add_argument("--dex-coins", type=int, default=DEX_COINS)
    parser.add_argument("--bridge-coins", type=int, default=BRIDGE_COINS)
    parser.add_argument("--bridge-claim-blocks", type=int, default=BRIDGE_CLAIM_BLOCKS)
    parser.add_argument("--wait-seconds", type=float, default=WAIT_SECONDS)
    parser.add_argument("--poll-seconds", type=float, default=POLL_SECONDS)
    parser.add_argument(
        "--no-faucet-endpoint",
        dest="use_faucet_endpoint",
        action="store_false",
        help="always sign the demo grants with the published faucet key",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Parse the command line, wait for the bootstrap, and seed the demo state."""
    parser = _parser()
    arguments = parser.parse_args(argv)
    for name in ("grant_coins", "dex_coins", "bridge_coins", "bridge_claim_blocks"):
        if getattr(arguments, name) < 1:
            parser.error(f"--{name.replace('_', '-')} must be at least 1")
    if arguments.wait_seconds <= 0:
        parser.error("--wait-seconds must be positive")
    if arguments.poll_seconds <= 0:
        parser.error("--poll-seconds must be positive")
    options = SeedOptions(
        grant_coins=arguments.grant_coins,
        dex_coins=arguments.dex_coins,
        bridge_coins=arguments.bridge_coins,
        bridge_claim_blocks=arguments.bridge_claim_blocks,
        wait_seconds=arguments.wait_seconds,
        poll_seconds=arguments.poll_seconds,
        use_faucet_endpoint=arguments.use_faucet_endpoint,
    )
    try:
        miners = load_stack(arguments.stack)
        with httpx.Client(base_url=arguments.node, timeout=30) as client:
            height = wait_for_bootstrap(client, wait_seconds=arguments.wait_seconds)
            print(f"seed: node is at bootstrap height {height}", flush=True)
            report = seed_once(client, miners=miners, options=options)
    except (SeedError, BootstrapError, traffic.TrafficError, httpx.HTTPError) as exc:
        print(f"seed: {exc}", file=sys.stderr)
        return 1
    print(f"seed: {report.bridge}", flush=True)
    print(f"seed: {report.dex}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
