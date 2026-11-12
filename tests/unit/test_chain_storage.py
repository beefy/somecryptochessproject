"""SQLite persistence, snapshots and restart replay (plan section 5.11)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from sccp.chain.chain import Chain
from sccp.chain.storage import SCHEMA_VERSION, Storage, persist_chain, restore_chain
from sccp.config import DEVNET, MAINNET
from sccp.errors import StorageError


@pytest.fixture()
def stored(live_chain: Any, tmp_path: Path) -> tuple[Chain, Storage]:
    """A persisted devnet chain in a temporary database file."""
    chain = live_chain.chain
    storage = Storage(tmp_path / "chain.db")
    persist_chain(chain, storage)
    return chain, storage


def test_persist_writes_every_block_and_the_tip(stored: tuple[Chain, Storage]) -> None:
    chain, storage = stored
    try:
        assert storage.block_count() == chain.height + 1
        assert storage.tip_hash() == chain.tip_hash
        assert storage.meta("network") == DEVNET.network_id
        assert storage.meta("schema_version") == str(SCHEMA_VERSION)
        heights = [block.header.height for block in storage.iter_blocks()]
        assert heights == sorted(heights)
        assert heights == list(range(chain.height + 1))
    finally:
        storage.close()


def test_the_genesis_snapshot_is_written(stored: tuple[Chain, Storage]) -> None:
    chain, storage = stored
    try:
        snapshot = storage.latest_snapshot(DEVNET)
        assert snapshot is not None
        assert snapshot.height == 0
        assert snapshot.state.state_root() == chain.genesis_state.state_root()
        assert snapshot.block_hash == chain.genesis_hash
    finally:
        storage.close()


def test_a_tip_snapshot_round_trips(stored: tuple[Chain, Storage]) -> None:
    chain, storage = stored
    try:
        storage.save_snapshot(chain.tip)
        snapshot = storage.latest_snapshot(DEVNET)
        assert snapshot is not None
        assert snapshot.height == chain.height
        assert snapshot.chain_work == chain.tip.chain_work
        assert snapshot.state.state_root() == chain.state.state_root()
    finally:
        storage.close()


def test_restore_replays_the_chain_and_keeps_the_state(stored: tuple[Chain, Storage]) -> None:
    chain, storage = stored
    try:
        assert storage.latest_snapshot(DEVNET) is not None
        restored = restore_chain(DEVNET, chain.genesis_block, chain.genesis_state, storage)
    finally:
        storage.close()
    assert restored.height == chain.height
    assert restored.tip_hash == chain.tip_hash
    assert restored.state.state_root() == chain.state.state_root()
    assert restored.state.minted == chain.state.minted


def test_a_tampered_snapshot_is_refused(stored: tuple[Chain, Storage], tmp_path: Path) -> None:
    chain, storage = stored
    path = tmp_path / "chain.db"
    storage.close()
    connection = sqlite3.connect(path)
    try:
        row = connection.execute("SELECT height, state_json FROM snapshots LIMIT 1").fetchone()
        assert row is not None
        height, payload = row
        document = json.loads(payload)
        for entry in document["accounts"].values():
            entry["balance"] = int(entry["balance"]) + 1
        connection.execute(
            "UPDATE snapshots SET state_json = ? WHERE height = ?",
            (json.dumps(document), height),
        )
        connection.commit()
    finally:
        connection.close()
    reopened = Storage(path)
    try:
        with pytest.raises(StorageError, match="disagrees with the replayed state"):
            restore_chain(DEVNET, chain.genesis_block, chain.genesis_state, reopened)
    finally:
        reopened.close()


def test_a_disagreeing_tip_is_refused(stored: tuple[Chain, Storage], tmp_path: Path) -> None:
    chain, storage = stored
    storage.close()
    path = tmp_path / "chain.db"
    connection = sqlite3.connect(path)
    try:
        connection.execute("UPDATE meta SET value = ? WHERE key = 'tip'", ("ab" * 32,))
        connection.commit()
    finally:
        connection.close()
    reopened = Storage(path)
    try:
        with pytest.raises(StorageError, match="not the heaviest block"):
            restore_chain(DEVNET, chain.genesis_block, chain.genesis_state, reopened)
    finally:
        reopened.close()


def test_a_corrupt_block_is_reported(stored: tuple[Chain, Storage], tmp_path: Path) -> None:
    _, storage = stored
    storage.close()
    path = tmp_path / "chain.db"
    connection = sqlite3.connect(path)
    try:
        connection.execute("UPDATE blocks SET block_json = '{}'")
        connection.commit()
    finally:
        connection.close()
    reopened = Storage(path)
    try:
        with pytest.raises(StorageError, match="corrupt"):
            list(reopened.iter_blocks())
    finally:
        reopened.close()


def test_one_database_cannot_hold_two_networks(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "bound.db")
    try:
        storage.bind_network(DEVNET)
        storage.bind_network(DEVNET)
        with pytest.raises(StorageError, match="holds devnet, not mainnet"):
            storage.bind_network(MAINNET)
    finally:
        storage.close()


def test_an_incompatible_schema_fails_loudly(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    storage = Storage(path)
    storage.set_meta("schema_version", str(SCHEMA_VERSION + 1))
    storage.close()
    with pytest.raises(StorageError, match="expects"):
        Storage(path)

