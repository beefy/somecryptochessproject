"""Genesis blocks and allocation documents (PROTOCOL.md sections 12-13).

Genesis is the only premine: a testnet faucet allocation or, on mainnet, no
allocation at all. Its hash is pinned in :class:`~sccp.config.NetworkParams` and
checked on load, so two nodes cannot disagree about height 0.

A genesis document is small, hand-auditable JSON::

    {
        "version": 1,
        "network": "testnet",
        "timestamp": 1700000000,
        "miner": "sccpt1...",  # optional; defaults to the first allocation
        "allocations": [{"address": "sccpt1...", "public_key": "hex", "amount": 5000000000}],
    }

The public key is mandatory for an allocation because Ed25519 is not
key-recoverable: without it the allocated coins could never be spent.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Final

from sccp.chain.block import HEADER_VERSION, block_hash, body_root
from sccp.chain.difficulty import initial_difficulty
from sccp.chain.state import ZERO_HASH, Account, State
from sccp.config import NetworkParams, get_network
from sccp.crypto.address import encode_address, validate_address
from sccp.crypto.pq import (
    HYBRID_PUBLIC_KEY_SIZE,
    HYBRID_SCHEME_ID,
    HybridKeypair,
    hybrid_keypair_from_seed,
)
from sccp.errors import GenesisError, KeyEncodingError
from sccp.protocol.models import Block, BlockHeader
from sccp.serialization import merkle_root_hex

GENESIS_DOCUMENT_VERSION: Final = 1
"""The only genesis document version this implementation accepts."""

FAUCET_SEEDS: Final[dict[str, bytes]] = {
    "testnet": b"sccp/testnet/faucet/v1",
    "devnet": b"sccp/devnet/faucet/v1",
}
"""Documented seeds for the test-network faucets.

These networks have no value, and a published key is what makes a test wallet
possible without a coordination step. Mainnet has no faucet and no premine, so
it must never gain an entry here.
"""

FAUCET_BALANCE: Final = 1_000_000 * 100_000_000
"""One million coins, in base units, allocated to each test-network faucet."""


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
    """One premine output: an address, its public key and an amount."""

    address: str
    public_key: str
    amount: int

    def as_dict(self) -> dict[str, object]:
        """Canonical document form."""
        return {
            "address": self.address,
            "public_key": self.public_key,
            "amount": self.amount,
        }


def zero_address(params: NetworkParams) -> str:
    """A valid, unspendable address used when genesis has no allocation."""
    # A well-formed hybrid blob nobody holds a private key for.
    return encode_address(
        bytes([HYBRID_SCHEME_ID]) + b"\x00" * (HYBRID_PUBLIC_KEY_SIZE - 1), params
    )


def _parse_allocations(params: NetworkParams, raw: object) -> tuple[GenesisAllocation, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise GenesisError("genesis allocations must be a list")
    allocations: list[GenesisAllocation] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, Mapping):
            raise GenesisError("each genesis allocation must be an object")
        extra = sorted(set(item) - {"address", "public_key", "amount"})
        if extra:
            raise GenesisError(f"unknown genesis allocation fields: {extra}")
        address = item.get("address")
        public_key = item.get("public_key")
        amount = item.get("amount")
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
        if address in seen:
            raise GenesisError(f"duplicate genesis allocation for {address}")
        seen.add(address)
        allocations.append(GenesisAllocation(address, public_key, amount))
    return tuple(allocations)


def _check_allocation(entry: GenesisAllocation, params: NetworkParams, seen: set[str]) -> None:
    """Every invariant an allocation must satisfy, whoever built it.

    The public key must derive the declared address: Ed25519 is not
    key-recoverable, so a mismatched pair would make the premine permanently
    unspendable and silently so.
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
    if entry.address in seen:
        raise GenesisError(f"duplicate genesis allocation for {entry.address}")
    seen.add(entry.address)


def build_genesis(
    network: str | NetworkParams,
    *,
    timestamp: int,
    allocations: Iterable[GenesisAllocation] = (),
    miner: str | None = None,
) -> tuple[Block, State]:
    """Build the height-0 block and the state it produces."""
    params = get_network(network)
    if timestamp < 0:
        raise GenesisError("genesis timestamp must be non-negative")
    entries = tuple(allocations)
    seen: set[str] = set()
    for entry in entries:
        _check_allocation(entry, params, seen)
    total = sum(entry.amount for entry in entries)
    cap = params.max_money
    if cap is not None and total > cap:
        raise GenesisError(f"genesis allocations exceed MAX_MONEY ({cap})")

    state = State(params=params, height=0, timestamps=[timestamp])
    for entry in entries:
        state.accounts[entry.address] = Account(balance=entry.amount, public_key=entry.public_key)
    state.minted = total
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
) -> dict[str, object]:
    """The on-disk document for a genesis, with its computed hash."""
    params = get_network(network)
    entries = tuple(allocations)
    block, _ = build_genesis(params, timestamp=timestamp, allocations=entries, miner=miner)
    document: dict[str, object] = {
        "version": GENESIS_DOCUMENT_VERSION,
        "network": params.network_id,
        "timestamp": timestamp,
        "hash": block_hash(block.header),
        "allocations": [entry.as_dict() for entry in entries],
    }
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
    block, state = build_genesis(params, timestamp=timestamp, allocations=allocations, miner=miner)
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
    "GenesisAllocation",
    "build_genesis",
    "faucet_allocation",
    "faucet_keypair",
    "genesis_document",
    "load_genesis",
    "parse_genesis_document",
    "zero_address",
]
