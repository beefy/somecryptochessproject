"""Genesis blocks and allocation documents (PROTOCOL.md section 13).

Genesis is the only place where supply can exist before a block subsidy. A
document can allocate native SCCP and any consensus-defined variant asset, and
can seed DEX pools or HTLC records so a test network starts with a usable
market and bridge example. Its hash is pinned in
:class:`~sccp.config.NetworkParams` and checked on load, so two nodes cannot
disagree about height 0.

A genesis document is small, hand-auditable JSON::

    {
        "version": 2,
        "network": "mainnet",
        "timestamp": 1735689600,
        "miner": "sccp1...",  # optional; defaults to the first allocation
        "allocations": [
            {"address": "sccp1...", "public_key": "hex", "amount": 4200000000000},
            {"address": "sccp1...", "public_key": "hex",
             "asset": "<64-hex C960 id>", "amount": 52500000000000}
        ],
        "pools": [{"creator": "sccp1...", "amount_a": 5, "asset_b": "<id>", "amount_b": 5}],
        "htlcs": [{"sender": "sccp1...", "beneficiary": "sccp1...", ...}],
    }

The public key is mandatory for an allocation because Ed25519 is not
key-recoverable: without it the allocated coins could never be spent. Native
SCCP omits ``asset``; a variant allocation names its derived 64-hex asset id.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Final

from sccp.chain import dex
from sccp.chain.block import HEADER_VERSION, block_hash, body_root
from sccp.chain.difficulty import initial_difficulty
from sccp.chain.state import ZERO_HASH, Account, HtlcRecord, HtlcStatus, Pool, State
from sccp.chain.tx import htlc_id
from sccp.config import UNITS_PER_COIN, NetworkParams, get_network
from sccp.crypto.address import encode_address, validate_address
from sccp.crypto.pq import (
    HYBRID_PUBLIC_KEY_SIZE,
    HYBRID_SCHEME_ID,
    HybridKeypair,
    hybrid_keypair_from_seed,
)
from sccp.errors import GenesisError, KeyEncodingError, StateError
from sccp.protocol.models import Block, BlockHeader
from sccp.serialization import merkle_root_hex

GENESIS_DOCUMENT_VERSION: Final = 2
"""The only genesis document version this implementation accepts."""

FAUCET_SEEDS: Final[dict[str, bytes]] = {
    "testnet": b"sccp/testnet/faucet/v1",
    "devnet": b"sccp/devnet/faucet/v1",
}
"""Documented seeds for the test-network faucets.

These networks have no value, and a published key is what makes a test wallet
possible without a coordination step. Mainnet has no faucet, so it must never
gain an entry here.
"""

FAUCET_BALANCE: Final = 1_000_000 * 100_000_000
"""One million coins, in base units, allocated to each test-network faucet."""

TESTNET_DEMO_SEED_NAMESPACE: Final = b"sccp/testnet/deploy-demo-wallet/"
"""Published seed namespace for the two wallets the testnet demo uses.

The genesis document and ``deploy/seed_testnet_demo.py`` derive the same keys
from this namespace, so the pool and bridge records committed at height 0 name
the wallets the deployment helper already knows how to sign for.
"""

TESTNET_BRIDGE_SECRET: Final = b"sccp/testnet/deploy-demo-bridge/"
"""Published preimage source for the testnet demo HTLC; worthless by design."""


def testnet_demo_keypair(index: int) -> HybridKeypair:
    """The deterministic keypair behind testnet demo wallet ``index``."""
    if index < 0:
        raise GenesisError("a demo wallet index must be non-negative")
    seed = sha256(TESTNET_DEMO_SEED_NAMESPACE + index.to_bytes(4, "big")).digest()
    return hybrid_keypair_from_seed(seed)


def testnet_bridge_preimage() -> str:
    """The published preimage of the testnet demo bridge HTLC."""
    return sha256(TESTNET_BRIDGE_SECRET).hexdigest()


def testnet_bridge_hashlock() -> str:
    """``sha256(preimage)`` for the testnet demo bridge HTLC."""
    return sha256(bytes.fromhex(testnet_bridge_preimage())).hexdigest()


def faucet_keypair(network: str) -> HybridKeypair:
    """The deterministic, public faucet keypair of a test network."""
    try:
        seed = FAUCET_SEEDS[network]
    except KeyError:
        raise GenesisError(f"{network} has no faucet; only test networks do") from None
    return hybrid_keypair_from_seed(sha256(seed).digest())


def faucet_allocation(network: str, params: NetworkParams) -> GenesisAllocation:
    """The faucet allocation a generated genesis document carries."""
    keypair = faucet_keypair(network)
    return GenesisAllocation(
        address=encode_address(keypair.public_key_bytes, params),
        public_key=keypair.public_key_hex,
        amount=FAUCET_BALANCE,
    )


@dataclass(frozen=True, slots=True)
class GenesisAllocation:
    """One genesis output: an address, its public key, an asset and an amount.

    ``asset`` is empty for native SCCP and a 64-hex variant asset id otherwise.
    The default keeps the three-argument constructor used by older callers and
    makes an omitted document field mean SCCP, exactly like a transfer payload.
    """

    address: str
    public_key: str
    amount: int
    asset: str = ""

    def as_dict(self) -> dict[str, object]:
        """Canonical document form; native SCCP omits ``asset``."""
        document: dict[str, object] = {
            "address": self.address,
            "public_key": self.public_key,
            "amount": self.amount,
        }
        if self.asset:
            document["asset"] = self.asset
        return document


@dataclass(frozen=True, slots=True)
class GenesisPool:
    """A DEX pool created at height 0 from its creator's allocation.

    The two amounts are debited from ``creator`` and become the initial
    reserves; the creator owns ``isqrt(amount_a * amount_b)`` LP shares. The
    side fields use the same convention as the transaction payload: an omitted
    asset is native SCCP.
    """

    creator: str
    asset_a: str
    amount_a: int
    asset_b: str
    amount_b: int

    def as_dict(self) -> dict[str, object]:
        """Canonical document form; native sides omit ``asset_*``."""
        document: dict[str, object] = {
            "creator": self.creator,
            "amount_a": self.amount_a,
            "amount_b": self.amount_b,
        }
        if self.asset_a:
            document["asset_a"] = self.asset_a
        if self.asset_b:
            document["asset_b"] = self.asset_b
        return document


@dataclass(frozen=True, slots=True)
class GenesisHtlc:
    """An HTLC record committed at height 0.

    A locked record holds its amount in consensus escrow; a claimed one has
    already paid the beneficiary, so genesis debits the sender and credits the
    beneficiary. A refunded record has no net ledger effect. The id is derived
    from ``sender`` and ``nonce`` exactly as a normal lock's id is.
    """

    sender: str
    beneficiary: str
    hashlock: str
    amount: int
    timelock_height: int
    asset: str = ""
    status: HtlcStatus = HtlcStatus.LOCKED
    preimage: str | None = None
    created_at_height: int = 0
    closed_at_height: int | None = None
    nonce: int = 0

    def as_dict(self) -> dict[str, object]:
        """Canonical document form."""
        document: dict[str, object] = {
            "sender": self.sender,
            "beneficiary": self.beneficiary,
            "hashlock": self.hashlock,
            "amount": self.amount,
            "timelock_height": self.timelock_height,
            "status": self.status.value,
            "created_at_height": self.created_at_height,
            "nonce": self.nonce,
        }
        if self.asset:
            document["asset"] = self.asset
        if self.closed_at_height is not None:
            document["closed_at_height"] = self.closed_at_height
        if self.preimage is not None:
            document["preimage"] = self.preimage
        return document


def zero_address(params: NetworkParams) -> str:
    """A valid, unspendable address used when genesis has no allocation."""
    # A well-formed hybrid blob nobody holds a private key for.
    return encode_address(
        bytes([HYBRID_SCHEME_ID]) + b"\x00" * (HYBRID_PUBLIC_KEY_SIZE - 1), params
    )


def _parse_asset(params: NetworkParams, raw: object, label: str) -> str:
    """Parse an optional asset id; omitted, null and ``SCCP`` all mean native."""
    if raw is None or raw == "SCCP" or raw == "":
        return ""
    if not isinstance(raw, str) or len(raw) != 64 or raw != raw.lower():
        raise GenesisError(f"{label} must be omitted for SCCP or a 32-byte lowercase asset id")
    try:
        bytes.fromhex(raw)
    except ValueError:
        raise GenesisError(f"{label} must be hexadecimal") from None
    if all(variant.asset != raw for variant in params.variants):
        raise GenesisError(f"{label} names unknown asset {raw}")
    return raw


def _parse_allocations(params: NetworkParams, raw: object) -> tuple[GenesisAllocation, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise GenesisError("genesis allocations must be a list")
    allocations: list[GenesisAllocation] = []
    seen: set[tuple[str, str]] = set()
    for item in raw:
        if not isinstance(item, Mapping):
            raise GenesisError("each genesis allocation must be an object")
        extra = sorted(set(item) - {"address", "public_key", "amount", "asset"})
        if extra:
            raise GenesisError(f"unknown genesis allocation fields: {extra}")
        address = item.get("address")
        public_key = item.get("public_key")
        amount = item.get("amount")
        asset = _parse_asset(params, item.get("asset"), f"allocation for {address}")
        if not isinstance(address, str) or not validate_address(address, params):
            raise GenesisError(f"{address!r} is not a {params.network_id} address")
        if not isinstance(public_key, str):
            raise GenesisError(f"allocation for {address} needs a public key")
        try:
            derived = encode_address(bytes.fromhex(public_key), params)
        except (KeyEncodingError, ValueError) as exc:
            raise GenesisError(f"allocation public key for {address} is invalid: {exc}") from None
        if derived != address:
            raise GenesisError(
                f"allocation public key derives {derived}, not the declared address {address}"
            )
        if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
            raise GenesisError(f"allocation for {address} needs a positive integer amount")
        if (address, asset) in seen:
            raise GenesisError(f"duplicate genesis allocation for {address} and {asset or 'SCCP'}")
        seen.add((address, asset))
        allocations.append(GenesisAllocation(address, public_key, amount, asset))
    return tuple(allocations)


def _check_allocation(
    entry: GenesisAllocation, params: NetworkParams, seen: set[tuple[str, str]]
) -> None:
    """Every invariant an allocation must satisfy, whoever built it.

    The public key must derive the declared address: Ed25519 is not
    key-recoverable, so a mismatched pair would make the premine permanently
    unspendable and silently so. One address may appear once per asset, which
    lets a treasury or testnet demo wallet hold SCCP and a variant token.
    """
    if not validate_address(entry.address, params):
        raise GenesisError(f"{entry.address!r} is not a {params.network_id} address")
    try:
        derived = encode_address(bytes.fromhex(entry.public_key), params)
    except (KeyEncodingError, ValueError) as exc:
        raise GenesisError(f"allocation public key for {entry.address} is invalid: {exc}") from None
    if derived != entry.address:
        raise GenesisError(
            f"allocation public key derives {derived}, not the declared address {entry.address}"
        )
    if not isinstance(entry.amount, int) or isinstance(entry.amount, bool) or entry.amount <= 0:
        raise GenesisError(f"allocation for {entry.address} needs a positive integer amount")
    _parse_asset(params, entry.asset, f"allocation for {entry.address}")
    key = (entry.address, entry.asset)
    if key in seen:
        raise GenesisError(
            f"duplicate genesis allocation for {entry.address} and {entry.asset or 'SCCP'}"
        )
    seen.add(key)


def _parse_pools(params: NetworkParams, raw: object) -> tuple[GenesisPool, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise GenesisError("genesis pools must be a list")
    pools: list[GenesisPool] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, Mapping):
            raise GenesisError("each genesis pool must be an object")
        extra = sorted(set(item) - {"creator", "asset_a", "amount_a", "asset_b", "amount_b"})
        if extra:
            raise GenesisError(f"unknown genesis pool fields: {extra}")
        creator = item.get("creator")
        if not isinstance(creator, str) or not validate_address(creator, params):
            raise GenesisError(f"pool creator {creator!r} is not a {params.network_id} address")
        asset_a = _parse_asset(params, item.get("asset_a"), "pool asset_a")
        asset_b = _parse_asset(params, item.get("asset_b"), "pool asset_b")
        amount_a = item.get("amount_a")
        amount_b = item.get("amount_b")
        if (
            not isinstance(amount_a, int)
            or isinstance(amount_a, bool)
            or amount_a <= 0
            or not isinstance(amount_b, int)
            or isinstance(amount_b, bool)
            or amount_b <= 0
        ):
            raise GenesisError("genesis pool amounts must be positive integers")
        if asset_a == asset_b:
            raise GenesisError("a genesis pool needs two different assets")
        asset_a, amount_a, asset_b, amount_b = dex.normalize_pair(
            asset_a, amount_a, asset_b, amount_b
        )
        if dex.initial_shares(amount_a, amount_b) <= 0:
            raise GenesisError("the genesis pool deposit is too small to mint a share")
        identifier = dex.pool_id(asset_a, asset_b)
        if identifier in seen:
            raise GenesisError(f"duplicate genesis pool {identifier}")
        seen.add(identifier)
        pools.append(GenesisPool(creator, asset_a, amount_a, asset_b, amount_b))
    return tuple(pools)


def _parse_htlcs(params: NetworkParams, raw: object) -> tuple[GenesisHtlc, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise GenesisError("genesis htlcs must be a list")
    htlcs: list[GenesisHtlc] = []
    seen: set[str] = set()
    fields = {
        "sender",
        "beneficiary",
        "hashlock",
        "amount",
        "timelock_height",
        "asset",
        "status",
        "preimage",
        "created_at_height",
        "closed_at_height",
        "nonce",
    }
    for item in raw:
        if not isinstance(item, Mapping):
            raise GenesisError("each genesis htlc must be an object")
        extra = sorted(set(item) - fields)
        if extra:
            raise GenesisError(f"unknown genesis htlc fields: {extra}")
        sender = item.get("sender")
        beneficiary = item.get("beneficiary")
        if not isinstance(sender, str) or not validate_address(sender, params):
            raise GenesisError(f"htlc sender {sender!r} is not a {params.network_id} address")
        if not isinstance(beneficiary, str) or not validate_address(beneficiary, params):
            raise GenesisError(
                f"htlc beneficiary {beneficiary!r} is not a {params.network_id} address"
            )
        hashlock = item.get("hashlock")
        if not isinstance(hashlock, str):
            raise GenesisError(f"htlc for {sender} needs a hashlock")
        amount = item.get("amount")
        timelock_height = item.get("timelock_height")
        nonce = item.get("nonce", 0)
        created_at_height = item.get("created_at_height", 0)
        closed_at_height = item.get("closed_at_height")
        if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
            raise GenesisError(f"htlc for {sender} needs a positive integer amount")
        if not isinstance(timelock_height, int) or isinstance(timelock_height, bool):
            raise GenesisError(f"htlc for {sender} needs an integer timelock_height")
        if not isinstance(nonce, int) or isinstance(nonce, bool) or nonce < 0:
            raise GenesisError(f"htlc for {sender} needs a non-negative integer nonce")
        if not isinstance(created_at_height, int) or created_at_height != 0:
            raise GenesisError("a genesis htlc must have created_at_height 0")
        if closed_at_height is not None and (
            not isinstance(closed_at_height, int)
            or isinstance(closed_at_height, bool)
            or closed_at_height != 0
        ):
            raise GenesisError("a closed genesis htlc must have closed_at_height 0")
        status_raw = item.get("status", HtlcStatus.LOCKED.value)
        try:
            status = HtlcStatus(status_raw)
        except ValueError:
            raise GenesisError(
                f"htlc status {status_raw!r} is not locked, claimed or refunded"
            ) from None
        preimage = item.get("preimage")
        if preimage is not None and not isinstance(preimage, str):
            raise GenesisError(f"htlc preimage for {sender} must be a string or null")
        if status is HtlcStatus.CLAIMED:
            if preimage is None:
                raise GenesisError("a claimed genesis htlc needs its preimage")
            try:
                digest = sha256(bytes.fromhex(preimage)).hexdigest()
            except ValueError:
                raise GenesisError("a claimed genesis htlc preimage must be hexadecimal") from None
            if digest != hashlock:
                raise GenesisError("a claimed genesis htlc preimage does not open its hashlock")
        elif preimage is not None:
            raise GenesisError("only a claimed genesis htlc may carry a preimage")
        if status is HtlcStatus.LOCKED and timelock_height <= 0:
            raise GenesisError("a locked genesis htlc needs a positive timelock height")
        record = GenesisHtlc(
            sender=sender,
            beneficiary=beneficiary,
            hashlock=hashlock,
            amount=amount,
            timelock_height=timelock_height,
            asset=_parse_asset(params, item.get("asset"), f"htlc for {sender}"),
            status=status,
            preimage=preimage,
            created_at_height=created_at_height,
            closed_at_height=closed_at_height,
            nonce=nonce,
        )
        identifier = htlc_id(sender, nonce)
        if identifier in seen:
            raise GenesisError(f"duplicate genesis htlc {identifier}")
        seen.add(identifier)
        htlcs.append(record)
    return tuple(htlcs)


def _debit_genesis(state: State, address: str, asset: str, amount: int) -> None:
    """Debit an allocation-backed pool or HTLC, with a readable error."""
    account = state.account(address)
    held = account.assets.get(asset, 0) if asset else account.balance
    if held < amount:
        label = asset or "SCCP"
        raise GenesisError(f"{address} holds {held} {label}, cannot allocate {amount}")
    if asset:
        state.debit_asset(address, asset, amount)
    else:
        state.ensure(address).balance -= amount


def _credit_genesis(state: State, address: str, asset: str, amount: int) -> None:
    """Credit a claimed genesis HTLC to its beneficiary."""
    if asset:
        state.credit_asset(address, asset, amount)
    else:
        state.ensure(address).balance += amount


def _apply_genesis_pool(state: State, entry: GenesisPool) -> None:
    """Apply one genesis pool, mirroring ``_apply_create_pool``."""
    if not validate_address(entry.creator, state.params):
        raise GenesisError(f"pool creator {entry.creator!r} is not a valid address")
    asset_a = _parse_asset(state.params, entry.asset_a, "pool asset_a")
    asset_b = _parse_asset(state.params, entry.asset_b, "pool asset_b")
    try:
        asset_a, amount_a, asset_b, amount_b = dex.normalize_pair(
            asset_a, entry.amount_a, asset_b, entry.amount_b
        )
    except ValueError as exc:
        raise GenesisError(f"invalid genesis pool pair: {exc}") from None
    identifier = dex.pool_id(asset_a, asset_b)
    if identifier in state.pools:
        raise GenesisError(f"duplicate genesis pool {identifier}")
    shares = dex.initial_shares(amount_a, amount_b)
    if shares <= 0:
        raise GenesisError("the genesis pool deposit is too small to mint a share")
    _debit_genesis(state, entry.creator, asset_a, amount_a)
    _debit_genesis(state, entry.creator, asset_b, amount_b)
    try:
        state.pools[identifier] = Pool(
            pool_id=identifier,
            asset_a=asset_a,
            asset_b=asset_b,
            reserve_a=amount_a,
            reserve_b=amount_b,
            total_shares=shares,
            shares={entry.creator: shares},
        )
    except StateError as exc:
        raise GenesisError(f"invalid genesis pool {identifier}: {exc}") from None


def _apply_genesis_htlc(state: State, entry: GenesisHtlc) -> None:
    """Apply one genesis HTLC, mirroring lock/claim/refund ledger effects."""
    if not validate_address(entry.sender, state.params):
        raise GenesisError(f"htlc sender {entry.sender!r} is not a valid address")
    if not validate_address(entry.beneficiary, state.params):
        raise GenesisError(f"htlc beneficiary {entry.beneficiary!r} is not a valid address")
    asset = _parse_asset(state.params, entry.asset, f"htlc for {entry.sender}")
    if entry.created_at_height != 0:
        raise GenesisError("a genesis htlc must have created_at_height 0")
    if entry.closed_at_height is not None and entry.closed_at_height != 0:
        raise GenesisError("a closed genesis htlc must have closed_at_height 0")
    if entry.status is HtlcStatus.CLAIMED:
        if entry.preimage is None:
            raise GenesisError("a claimed genesis htlc needs its preimage")
        try:
            digest = sha256(bytes.fromhex(entry.preimage)).hexdigest()
        except ValueError:
            raise GenesisError("a claimed genesis htlc preimage must be hexadecimal") from None
        if digest != entry.hashlock:
            raise GenesisError("a claimed genesis htlc preimage does not open its hashlock")
    elif entry.preimage is not None:
        raise GenesisError("only a claimed genesis htlc may carry a preimage")
    if entry.status is HtlcStatus.LOCKED and entry.timelock_height <= 0:
        raise GenesisError("a locked genesis htlc needs a positive timelock height")
    identifier = htlc_id(entry.sender, entry.nonce)
    if identifier in state.htlcs:
        raise GenesisError(f"duplicate genesis htlc {identifier}")
    if entry.status is HtlcStatus.LOCKED:
        _debit_genesis(state, entry.sender, asset, entry.amount)
    elif entry.status is HtlcStatus.CLAIMED:
        _debit_genesis(state, entry.sender, asset, entry.amount)
        _credit_genesis(state, entry.beneficiary, asset, entry.amount)
    else:
        # A refunded record has no net effect; the allocation already reflects
        # the returned amount, but the sender must still have been able to lock it.
        _debit_genesis(state, entry.sender, asset, entry.amount)
        _credit_genesis(state, entry.sender, asset, entry.amount)
    try:
        state.htlcs[identifier] = HtlcRecord(
            htlc_id=identifier,
            sender=entry.sender,
            beneficiary=entry.beneficiary,
            hashlock=entry.hashlock,
            timelock_height=entry.timelock_height,
            amount=entry.amount,
            created_at_height=entry.created_at_height,
            asset=asset,
            status=entry.status,
            closed_at_height=entry.closed_at_height,
            preimage=entry.preimage,
        )
    except StateError as exc:
        raise GenesisError(f"invalid genesis htlc {identifier}: {exc}") from None


def build_genesis(
    network: str | NetworkParams,
    *,
    timestamp: int,
    allocations: Iterable[GenesisAllocation] = (),
    miner: str | None = None,
    pools: Iterable[GenesisPool] = (),
    htlcs: Iterable[GenesisHtlc] = (),
) -> tuple[Block, State]:
    """Build the height-0 block and the state it produces."""
    params = get_network(network)
    if timestamp < 0:
        raise GenesisError("genesis timestamp must be non-negative")
    entries = tuple(
        GenesisAllocation(
            entry.address,
            entry.public_key,
            entry.amount,
            _parse_asset(params, entry.asset, f"allocation for {entry.address}"),
        )
        for entry in allocations
    )
    seen: set[tuple[str, str]] = set()
    for entry in entries:
        _check_allocation(entry, params, seen)
    native_total = sum(entry.amount for entry in entries if not entry.asset)
    variant_totals: dict[str, int] = {}
    for entry in entries:
        if entry.asset:
            variant_totals[entry.asset] = variant_totals.get(entry.asset, 0) + entry.amount
    cap = params.max_money
    if cap is not None and native_total > cap:
        raise GenesisError(f"genesis allocations exceed MAX_MONEY ({cap})")
    for asset, total in variant_totals.items():
        selected = params.variant_for_asset(asset)
        if selected.max_supply is None:
            continue
        asset_cap = selected.max_supply * UNITS_PER_COIN
        if total > asset_cap:
            raise GenesisError(
                f"genesis {selected.symbol} allocations exceed its cap ({asset_cap})"
            )

    state = State(params=params, height=0, timestamps=[timestamp])
    for entry in entries:
        account = state.accounts.get(entry.address)
        if account is None:
            account = Account(public_key=entry.public_key)
            state.accounts[entry.address] = account
        elif account.public_key is not None and account.public_key != entry.public_key:
            raise GenesisError(f"conflicting public keys for {entry.address}")
        else:
            account.public_key = entry.public_key
        if entry.asset:
            account.assets[entry.asset] = account.assets.get(entry.asset, 0) + entry.amount
        else:
            account.balance += entry.amount
    state.minted = native_total
    state.minted_assets = dict(sorted(variant_totals.items()))
    for pool in tuple(pools):
        _apply_genesis_pool(state, pool)
    for htlc in tuple(htlcs):
        _apply_genesis_htlc(state, htlc)
    state.epochs[0] = state.open_epoch(0, beacon=None, height=0)

    genesis_miner = miner or (entries[0].address if entries else zero_address(params))
    if not validate_address(genesis_miner, params):
        raise GenesisError(f"genesis miner {genesis_miner!r} is not a valid address")
    difficulty = initial_difficulty(params)
    header = BlockHeader(
        version=HEADER_VERSION,
        network=params.network_id,
        height=0,
        prev_hash=ZERO_HASH,
        merkle_root=body_root(()),
        state_root=state.state_root(),
        roster_root=merkle_root_hex({}),
        timestamp=timestamp,
        epoch=0,
        beacon_hash=ZERO_HASH,
        wins_required=difficulty.wins_required,
        seal_bits=difficulty.seal_bits,
        seal_nonce=0,
        miner=genesis_miner,
    )
    block = Block(header=header)
    state.tip_hash = block_hash(header)
    return block, state


def genesis_document(
    network: str | NetworkParams,
    *,
    timestamp: int,
    allocations: Iterable[GenesisAllocation] = (),
    miner: str | None = None,
    pools: Iterable[GenesisPool] = (),
    htlcs: Iterable[GenesisHtlc] = (),
) -> dict[str, object]:
    """The on-disk document for a genesis, with its computed hash."""
    params = get_network(network)
    entries = tuple(allocations)
    pool_entries = tuple(pools)
    htlc_entries = tuple(htlcs)
    block, _ = build_genesis(
        params,
        timestamp=timestamp,
        allocations=entries,
        miner=miner,
        pools=pool_entries,
        htlcs=htlc_entries,
    )
    document: dict[str, object] = {
        "version": GENESIS_DOCUMENT_VERSION,
        "network": params.network_id,
        "timestamp": timestamp,
        "hash": block_hash(block.header),
        "allocations": [entry.as_dict() for entry in entries],
    }
    if pool_entries:
        document["pools"] = [entry.as_dict() for entry in pool_entries]
    if htlc_entries:
        document["htlcs"] = [entry.as_dict() for entry in htlc_entries]
    if miner is not None:
        document["miner"] = miner
    return document


def parse_genesis_document(
    network: str | NetworkParams, document: Mapping[str, object]
) -> tuple[Block, State]:
    """Validate a genesis document and build its block and state."""
    params = get_network(network)
    if document.get("version") != GENESIS_DOCUMENT_VERSION:
        raise GenesisError(f"unsupported genesis document version {document.get('version')!r}")
    if document.get("network") != params.network_id:
        raise GenesisError(
            f"genesis document is for {document.get('network')!r}, "
            f"the parameters are {params.network_id!r}"
        )
    timestamp = document.get("timestamp")
    if not isinstance(timestamp, int) or isinstance(timestamp, bool):
        raise GenesisError("genesis timestamp must be an integer")
    miner = document.get("miner")
    if miner is not None and not isinstance(miner, str):
        raise GenesisError("genesis miner must be an address string")
    allocations = _parse_allocations(params, document.get("allocations"))
    pools = _parse_pools(params, document.get("pools"))
    htlcs = _parse_htlcs(params, document.get("htlcs"))
    block, state = build_genesis(
        params,
        timestamp=timestamp,
        allocations=allocations,
        miner=miner,
        pools=pools,
        htlcs=htlcs,
    )
    pinned = params.genesis_hash
    actual = block_hash(block.header)
    if pinned is not None and pinned != actual:
        raise GenesisError(
            f"genesis hash {actual} does not match the pinned {pinned} for {params.network_id}"
        )
    declared = document.get("hash")
    if declared is not None and declared != actual:
        raise GenesisError(f"genesis document declares hash {declared!r}, it computes {actual}")
    return block, state


def load_genesis(path: str | Path, network: str | NetworkParams) -> tuple[Block, State]:
    """Read a genesis JSON file and build its block and state."""
    params = get_network(network)
    file_path = Path(path)
    try:
        text = file_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise GenesisError(f"cannot read genesis file {file_path}: {exc}") from exc
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise GenesisError(f"genesis file {file_path} is not valid JSON: {exc}") from exc
    if not isinstance(document, Mapping):
        raise GenesisError(f"genesis file {file_path} must contain an object")
    return parse_genesis_document(params, document)


__all__ = [
    "FAUCET_BALANCE",
    "FAUCET_SEEDS",
    "GENESIS_DOCUMENT_VERSION",
    "TESTNET_BRIDGE_SECRET",
    "TESTNET_DEMO_SEED_NAMESPACE",
    "GenesisAllocation",
    "GenesisHtlc",
    "GenesisPool",
    "build_genesis",
    "faucet_allocation",
    "faucet_keypair",
    "genesis_document",
    "load_genesis",
    "parse_genesis_document",
    "testnet_bridge_hashlock",
    "testnet_bridge_preimage",
    "testnet_demo_keypair",
    "zero_address",
]
