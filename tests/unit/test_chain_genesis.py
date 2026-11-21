"""Genesis documents, pinned hashes and the faucet (PROTOCOL.md sections 12-13)."""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

import pytest

from sccp.chain import dex, genesis
from sccp.chain.genesis import (
    FAUCET_SEEDS,
    GENESIS_DOCUMENT_VERSION,
    GenesisAllocation,
    GenesisHtlc,
    GenesisPool,
    build_genesis,
    faucet_allocation,
    faucet_keypair,
    genesis_document,
    load_genesis,
    parse_genesis_document,
    zero_address,
)
from sccp.chain.state import HtlcStatus
from sccp.config import (
    CHESS960_ASSET_ID,
    DEVNET,
    MAINNET,
    TESTNET,
    UNITS_PER_COIN,
    NetworkParams,
)
from sccp.crypto.address import encode_address, validate_address
from sccp.crypto.pq import hybrid_keypair_from_seed
from sccp.errors import GenesisError

GENESIS_DIR = Path(__file__).resolve().parents[2] / "genesis"
COINS = 100_000_000

UNPINNED = TESTNET.model_copy(update={"genesis_hash": None})
"""Testnet parameters without the pinned hash, for document round-trip tests."""


def _allocation(
    amount: int = 1_000 * COINS,
    params: NetworkParams = TESTNET,
    asset: str = "",
    index: int = 0,
) -> GenesisAllocation:
    keypair = (
        faucet_keypair("testnet")
        if index == 0
        else hybrid_keypair_from_seed(bytes([0x50 + index]) * 32)
    )
    return GenesisAllocation(
        address=encode_address(keypair.public_key_bytes, params),
        public_key=keypair.public_key_hex,
        amount=amount,
        asset=asset,
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


def test_variant_allocations_are_tracked_in_their_own_asset() -> None:
    native = _allocation(10 * COINS)
    variant = _allocation(20 * COINS, asset=CHESS960_ASSET_ID)
    block, state = build_genesis(
        TESTNET,
        timestamp=1_700_000_000,
        allocations=(native, variant),
    )
    account = state.account(native.address)
    assert block.header.state_root == state.state_root()
    assert state.minted == native.amount
    assert state.minted_assets == {CHESS960_ASSET_ID: variant.amount}
    assert account.balance == native.amount
    assert account.assets == {CHESS960_ASSET_ID: variant.amount}


def test_a_variant_allocation_is_clamped_by_its_own_cap() -> None:
    variant = MAINNET.variant_for_asset(CHESS960_ASSET_ID)
    assert variant.max_supply is not None
    cap = variant.max_supply * UNITS_PER_COIN
    with pytest.raises(GenesisError, match="C960 allocations exceed"):
        build_genesis(
            MAINNET,
            timestamp=1_700_000_000,
            allocations=(_allocation(cap + 1, MAINNET, CHESS960_ASSET_ID),),
        )


def test_an_unknown_allocation_asset_is_rejected() -> None:
    allocation = _allocation()
    document = genesis_document(UNPINNED, timestamp=1_700_000_000, allocations=(allocation,))
    document["allocations"] = [{**allocation.as_dict(), "asset": "ab" * 32}]
    with pytest.raises(GenesisError, match="unknown asset"):
        parse_genesis_document(UNPINNED, document)


def test_a_genesis_pool_is_created_from_its_creators_allocation() -> None:
    creator = _allocation(10 * COINS)
    variant = _allocation(10 * COINS, asset=CHESS960_ASSET_ID)
    pool = GenesisPool(
        creator=creator.address,
        asset_a="",
        amount_a=10 * COINS,
        asset_b=CHESS960_ASSET_ID,
        amount_b=10 * COINS,
    )
    _block, state = build_genesis(
        TESTNET,
        timestamp=1_700_000_000,
        allocations=(creator, variant),
        pools=(pool,),
    )
    identifier = dex.pool_id("", CHESS960_ASSET_ID)
    stored = state.pools[identifier]
    assert stored.reserve_a == stored.reserve_b == 10 * COINS
    assert stored.total_shares == dex.initial_shares(10 * COINS, 10 * COINS)
    assert stored.shares == {creator.address: stored.total_shares}
    assert state.account(creator.address).balance == 0
    assert state.account(creator.address).assets.get(CHESS960_ASSET_ID, 0) == 0


def test_a_genesis_pool_cannot_spend_funds_that_were_not_allocated() -> None:
    creator = _allocation(5 * COINS)
    pool = GenesisPool(
        creator=creator.address,
        asset_a="",
        amount_a=10 * COINS,
        asset_b=CHESS960_ASSET_ID,
        amount_b=10 * COINS,
    )
    with pytest.raises(GenesisError, match="holds 500000000 ZQWX"):
        build_genesis(
            TESTNET,
            timestamp=1_700_000_000,
            allocations=(creator,),
            pools=(pool,),
        )


def test_a_claimed_genesis_htlc_pays_the_beneficiary() -> None:
    sender = _allocation(10 * COINS)
    beneficiary = _allocation(COINS, index=1)
    preimage = genesis.testnet_bridge_preimage()
    record = GenesisHtlc(
        sender=sender.address,
        beneficiary=beneficiary.address,
        hashlock=genesis.testnet_bridge_hashlock(),
        amount=COINS,
        timelock_height=100,
        status=HtlcStatus.CLAIMED,
        preimage=preimage,
        closed_at_height=0,
        nonce=7,
    )
    _block, state = build_genesis(
        TESTNET,
        timestamp=1_700_000_000,
        allocations=(sender, beneficiary),
        htlcs=(record,),
    )
    assert len(state.htlcs) == 1
    stored = next(iter(state.htlcs.values()))
    assert stored.status is HtlcStatus.CLAIMED
    assert stored.preimage == preimage
    assert state.account(sender.address).balance == 9 * COINS
    assert state.account(beneficiary.address).balance == 2 * COINS


def test_a_locked_genesis_htlc_holds_its_amount_in_escrow() -> None:
    sender = _allocation(10 * COINS)
    beneficiary = _allocation(COINS, index=1)
    record = GenesisHtlc(
        sender=sender.address,
        beneficiary=beneficiary.address,
        hashlock=genesis.testnet_bridge_hashlock(),
        amount=COINS,
        timelock_height=100,
        nonce=8,
    )
    _block, state = build_genesis(
        TESTNET,
        timestamp=1_700_000_000,
        allocations=(sender, beneficiary),
        htlcs=(record,),
    )
    assert next(iter(state.htlcs.values())).status is HtlcStatus.LOCKED
    assert state.account(sender.address).balance == 9 * COINS
    assert state.account(beneficiary.address).balance == COINS


def test_a_claimed_genesis_htlc_must_open_its_hashlock() -> None:
    sender = _allocation(10 * COINS)
    beneficiary = _allocation(COINS, index=1)
    record = GenesisHtlc(
        sender=sender.address,
        beneficiary=beneficiary.address,
        hashlock=sha256(b"a different secret").hexdigest(),
        amount=COINS,
        timelock_height=100,
        status=HtlcStatus.CLAIMED,
        preimage=genesis.testnet_bridge_preimage(),
        closed_at_height=0,
        nonce=10,
    )
    with pytest.raises(GenesisError, match="does not open"):
        build_genesis(
            TESTNET,
            timestamp=1_700_000_000,
            allocations=(sender, beneficiary),
            htlcs=(record,),
        )


def test_the_document_round_trips_pools_and_htlcs() -> None:
    creator = _allocation(11 * COINS)
    variant = _allocation(10 * COINS, asset=CHESS960_ASSET_ID)
    beneficiary = _allocation(COINS, index=1)
    pool = GenesisPool(
        creator=creator.address,
        asset_a="",
        amount_a=10 * COINS,
        asset_b=CHESS960_ASSET_ID,
        amount_b=10 * COINS,
    )
    htlc = GenesisHtlc(
        sender=creator.address,
        beneficiary=beneficiary.address,
        hashlock=genesis.testnet_bridge_hashlock(),
        amount=COINS,
        timelock_height=100,
        status=HtlcStatus.CLAIMED,
        preimage=genesis.testnet_bridge_preimage(),
        closed_at_height=0,
        nonce=9,
    )
    document = genesis_document(
        UNPINNED,
        timestamp=1_700_000_000,
        allocations=(creator, variant, beneficiary),
        pools=(pool,),
        htlcs=(htlc,),
    )
    assert document["pools"] == [pool.as_dict()]
    assert document["htlcs"] == [htlc.as_dict()]
    _block, state = parse_genesis_document(UNPINNED, document)
    assert len(state.pools) == 1
    assert next(iter(state.htlcs.values())).status is HtlcStatus.CLAIMED


def test_the_committed_mainnet_premine_is_one_basis_point() -> None:
    _block, state = load_genesis(GENESIS_DIR / "mainnet.json", MAINNET)
    assert MAINNET.max_money is not None
    assert state.minted == MAINNET.max_money * 1 // 10_000
    assert state.minted == 2_100 * COINS
    assert state.minted_assets == {}


def test_testnet_genesis_seeds_a_pool_and_a_completed_bridge() -> None:
    _block, state = load_genesis(GENESIS_DIR / "testnet.json", TESTNET)
    identifier = dex.pool_id("", CHESS960_ASSET_ID)
    pool = state.pools[identifier]
    assert pool.reserve_a == pool.reserve_b == 5 * COINS
    assert pool.total_shares == 5 * COINS
    assert len(state.htlcs) == 1
    record = next(iter(state.htlcs.values()))
    assert record.status is HtlcStatus.CLAIMED
    assert record.amount == COINS
    assert record.preimage == genesis.testnet_bridge_preimage()
    expected = encode_address(genesis.testnet_demo_keypair(0).public_key_bytes, TESTNET)
    assert pool.shares == {expected: pool.total_shares}
    assert record.sender == expected
