"""Genesis documents, pinned hashes and the faucet (PROTOCOL.md sections 12-13)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sccp.chain.genesis import (
    FAUCET_SEEDS,
    GENESIS_DOCUMENT_VERSION,
    GenesisAllocation,
    build_genesis,
    faucet_allocation,
    faucet_keypair,
    genesis_document,
    load_genesis,
    parse_genesis_document,
    zero_address,
)
from sccp.config import DEVNET, MAINNET, TESTNET, NetworkParams
from sccp.crypto.address import encode_address, validate_address
from sccp.crypto.pq import hybrid_keypair_from_seed
from sccp.errors import GenesisError

GENESIS_DIR = Path(__file__).resolve().parents[2] / "genesis"
COINS = 100_000_000

UNPINNED = TESTNET.model_copy(update={"genesis_hash": None})
"""Testnet parameters without the pinned hash, for document round-trip tests."""


def _allocation(amount: int = 1_000 * COINS, params: NetworkParams = TESTNET) -> GenesisAllocation:
    keypair = faucet_keypair("testnet")
    return GenesisAllocation(
        address=encode_address(keypair.public_key_bytes, params),
        public_key=keypair.public_key_hex,
        amount=amount,
    )


def test_building_a_genesis_is_deterministic() -> None:
    allocation = _allocation()
    first, state = build_genesis(TESTNET, timestamp=1_700_000_000, allocations=(allocation,))
    second, _ = build_genesis(TESTNET, timestamp=1_700_000_000, allocations=(allocation,))
    assert first == second
    assert first.header.height == 0
    assert first.header.prev_hash == "00" * 32
    assert first.header.miner == allocation.address
    assert first.coinbase is None
    assert state.minted == allocation.amount
    assert state.account(allocation.address).balance == allocation.amount
    assert state.account(allocation.address).public_key == allocation.public_key
    assert state.epochs[0].matchable is False


def test_an_allocation_needs_a_valid_public_key() -> None:
    allocation = _allocation()
    with pytest.raises(GenesisError, match="public key"):
        build_genesis(
            TESTNET,
            timestamp=1_700_000_000,
            allocations=(
                GenesisAllocation(
                    address=allocation.address, public_key="not-hex", amount=allocation.amount
                ),
            ),
        )
    stranger = faucet_keypair("testnet")
    other = encode_address(hybrid_keypair_from_seed(bytes([7]) * 32).public_key_bytes, TESTNET)
    with pytest.raises(GenesisError, match="derives"):
        build_genesis(
            TESTNET,
            timestamp=1_700_000_000,
            allocations=(
                GenesisAllocation(
                    address=other,
                    public_key=stranger.public_key_hex,
                    amount=allocation.amount,
                ),
            ),
        )
    with pytest.raises(GenesisError, match="positive integer"):
        build_genesis(
            TESTNET,
            timestamp=1_700_000_000,
            allocations=(GenesisAllocation(allocation.address, allocation.public_key, 0),),
        )


def test_genesis_rejects_duplicates_oversupply_and_bad_miners() -> None:
    allocation = _allocation()
    with pytest.raises(GenesisError, match="duplicate"):
        build_genesis(TESTNET, timestamp=1_700_000_000, allocations=(allocation, allocation))
    with pytest.raises(GenesisError, match="MAX_MONEY"):
        build_genesis(
            MAINNET,
            timestamp=1_700_000_000,
            allocations=(_allocation(MAINNET.max_money + 1, MAINNET),),  # type: ignore[operator]
        )
    with pytest.raises(GenesisError, match="non-negative"):
        build_genesis(TESTNET, timestamp=-1)
    with pytest.raises(GenesisError, match="not a valid address"):
        build_genesis(TESTNET, timestamp=1_700_000_000, miner="sccpt1nope")


def test_a_genesis_without_allocations_uses_the_zero_address() -> None:
    block, state = build_genesis(MAINNET, timestamp=1_700_000_000)
    assert block.header.miner == zero_address(MAINNET)
    assert validate_address(block.header.miner, MAINNET) is True
    assert state.minted == 0
    assert state.accounts == {}


def test_the_document_round_trips() -> None:
    allocation = _allocation()
    document = genesis_document(UNPINNED, timestamp=1_700_000_000, allocations=(allocation,))
    assert document["version"] == GENESIS_DOCUMENT_VERSION
    assert document["network"] == TESTNET.network_id
    assert document["timestamp"] == 1_700_000_000
    assert document["allocations"] == [allocation.as_dict()]
    assert isinstance(document["hash"], str) and len(document["hash"]) == 64
    block, state = parse_genesis_document(UNPINNED, document)
    assert state.account(allocation.address).balance == allocation.amount
    assert block.header.miner == allocation.address


def test_the_document_rejects_every_kind_of_tampering() -> None:
    document = genesis_document(UNPINNED, timestamp=1_700_000_000, allocations=(_allocation(),))
    cases = {
        "version": {"version": 99},
        "network": {"network": "mainnet"},
        "timestamp": {"timestamp": "yesterday"},
        "hash": {"hash": "00" * 32},
        "miner": {"miner": 7},
        "allocations": {"allocations": [{"address": "sccpt1nope"}]},
    }
    for field, patch in cases.items():
        broken = {**document, **patch}
        with pytest.raises(GenesisError):
            parse_genesis_document(UNPINNED, broken)
        assert field in broken


def test_a_pinned_network_refuses_a_foreign_document() -> None:
    document = genesis_document(UNPINNED, timestamp=1_700_000_000, allocations=(_allocation(),))
    with pytest.raises(GenesisError, match="does not match the pinned"):
        parse_genesis_document(TESTNET, document)


def test_loading_from_disk_reports_bad_files(tmp_path: Path) -> None:
    with pytest.raises(GenesisError, match="cannot read"):
        load_genesis(tmp_path / "missing.json", TESTNET)
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    with pytest.raises(GenesisError, match="not valid JSON"):
        load_genesis(broken, TESTNET)


def test_the_committed_files_match_the_generator() -> None:
    for name, params in (("devnet", DEVNET), ("testnet", TESTNET), ("mainnet", MAINNET)):
        path = GENESIS_DIR / f"{name}.json"
        stored = json.loads(path.read_text(encoding="utf-8"))
        assert stored["network"] == params.network_id
        _block, _state = load_genesis(path, params)
        assert stored["hash"] == params.genesis_hash


def test_the_faucet_is_public_and_test_only() -> None:
    assert set(FAUCET_SEEDS) == {"testnet", "devnet"}
    with pytest.raises(GenesisError, match="no faucet"):
        faucet_keypair("mainnet")
    testnet = faucet_allocation("testnet", TESTNET)
    devnet = faucet_allocation("devnet", DEVNET)
    assert testnet.address != devnet.address
    assert testnet.amount == devnet.amount == 1_000_000 * COINS
    assert testnet.public_key == faucet_keypair("testnet").public_key_hex
