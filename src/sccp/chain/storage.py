"""SQLite persistence with snapshots (PROTOCOL.md sections 12-13).

The store keeps three things: canonical block JSON, the adopted tip, and periodic
state snapshots. A restart replays blocks from the last snapshot, so it never
trusts the stored state blindly -- the snapshot is only a cache and its
``state_root`` is checked against the replayed state.

WAL mode is enabled so a node can read while a block is being written, and a
schema version is stored in ``meta`` so an incompatible database fails loudly
instead of being misread.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

from sccp.chain.chain import Chain, StoredBlock
from sccp.chain.state import State
from sccp.config import NetworkParams
from sccp.errors import StorageError
from sccp.protocol.models import Block

SCHEMA_VERSION = 1
"""Bumped whenever the on-disk layout changes incompatibly."""

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS blocks (
    hash TEXT PRIMARY KEY,
    height INTEGER NOT NULL,
    parent_hash TEXT NOT NULL,
    chain_work TEXT NOT NULL,
    block_json TEXT NOT NULL,
    tip INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS blocks_height ON blocks(height);
CREATE TABLE IF NOT EXISTS snapshots (
    height INTEGER PRIMARY KEY,
    hash TEXT NOT NULL,
    state_json TEXT NOT NULL
);
"""


class Storage:
    """A thin, explicit wrapper around one SQLite database file.

    Pass ``":memory:"`` for tests. The connection is opened in autocommit mode and
    every write is wrapped in an explicit transaction.
    """

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        try:
            # A node opens the store on its main thread and accepts blocks from
            # FastAPI's sync worker threads. NodeService.write_lock serialises
            # every mutation, so the connection may safely be shared across
            # threads; without this flag SQLite refuses those writes outright.
            self._connection = sqlite3.connect(
                self.path, isolation_level=None, check_same_thread=False
            )
        except sqlite3.Error as exc:  # pragma: no cover - platform dependent
            raise StorageError(f"cannot open {self.path}: {exc}") from exc
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=NORMAL")
        self._connection.executescript(_SCHEMA)
        self._check_version()

    # -- lifecycle ----------------------------------------------------------

    def _check_version(self) -> None:
        current = self.meta("schema_version")
        if current is None:
            self.set_meta("schema_version", str(SCHEMA_VERSION))
            return
        if current != str(SCHEMA_VERSION):
            raise StorageError(
                f"database {self.path} uses schema {current}, this build expects {SCHEMA_VERSION}"
            )

    def close(self) -> None:
        """Close the database handle."""
        self._connection.close()

    def __enter__(self) -> Storage:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # -- metadata -----------------------------------------------------------

    def meta(self, key: str) -> str | None:
        """Read one metadata value."""
        row = self._connection.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row[0])

    def set_meta(self, key: str, value: str) -> None:
        """Write one metadata value."""
        self._connection.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def bind_network(self, params: NetworkParams) -> None:
        """Refuse to reuse one database for two networks."""
        stored = self.meta("network")
        if stored is None:
            self.set_meta("network", params.network_id)
            return
        if stored != params.network_id:
            raise StorageError(f"database {self.path} holds {stored}, not {params.network_id}")

    # -- writes -------------------------------------------------------------

    def save_block(self, stored: StoredBlock, *, tip: bool) -> None:
        """Insert one block and optionally mark it as the tip."""
        payload = json.dumps(
            stored.block.model_dump(mode="json", by_alias=True),
            sort_keys=True,
            separators=(",", ":"),
        )
        with self._connection:
            self._connection.execute(
                "INSERT OR REPLACE INTO blocks"
                "(hash, height, parent_hash, chain_work, block_json, tip) "
                "VALUES(?, ?, ?, ?, ?, 0)",
                (
                    stored.block_hash,
                    stored.height,
                    stored.parent_hash,
                    str(stored.chain_work),
                    payload,
                ),
            )
            if tip:
                self._connection.execute("UPDATE blocks SET tip = 0")
                self._connection.execute(
                    "UPDATE blocks SET tip = 1 WHERE hash = ?", (stored.block_hash,)
                )
                self.set_meta("tip", stored.block_hash)
                self.set_meta("height", str(stored.height))

    def save_snapshot(self, stored: StoredBlock) -> None:
        """Persist a state snapshot for ``stored`` (cache only, never trusted)."""
        payload = json.dumps(stored.state.snapshot(), sort_keys=True, separators=(",", ":"))
        self._connection.execute(
            "INSERT OR REPLACE INTO snapshots(height, hash, state_json) VALUES(?, ?, ?)",
            (stored.height, stored.block_hash, payload),
        )

    # -- reads --------------------------------------------------------------

    def block_count(self) -> int:
        """How many blocks are stored."""
        row = self._connection.execute("SELECT COUNT(*) FROM blocks").fetchone()
        return int(row[0])

    def tip_hash(self) -> str | None:
        """The persisted tip hash, if any."""
        return self.meta("tip")

    def iter_blocks(self) -> Iterator[Block]:
        """Every stored block, lowest height first."""
        cursor = self._connection.execute(
            "SELECT block_json FROM blocks ORDER BY height ASC, hash ASC"
        )
        for (payload,) in cursor:
            try:
                yield Block.model_validate(json.loads(payload))
            except ValueError as exc:
                raise StorageError(f"stored block is corrupt: {exc}") from exc

    def latest_snapshot(self, params: NetworkParams) -> StoredBlock | None:
        """The highest stored snapshot, rebuilt as a partial :class:`StoredBlock`."""
        row = self._connection.execute(
            "SELECT height, hash, state_json FROM snapshots ORDER BY height DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        height, block_hash, state_json = row
        try:
            document = json.loads(state_json)
            state = State.from_snapshot(params, document)
        except (ValueError, KeyError) as exc:
            raise StorageError(f"stored snapshot at height {height} is corrupt: {exc}") from exc
        block_row = self._connection.execute(
            "SELECT block_json, parent_hash, chain_work FROM blocks WHERE hash = ?",
            (block_hash,),
        ).fetchone()
        if block_row is None:
            raise StorageError(f"snapshot names an unknown block {block_hash}")
        block = Block.model_validate(json.loads(block_row[0]))
        return StoredBlock(
            block=block,
            block_hash=block_hash,
            parent_hash=block_row[1],
            height=height,
            chain_work=int(block_row[2]),
            state=state,
        )


def persist_chain(chain: Chain, storage: Storage, *, tip: StoredBlock | None = None) -> int:
    """Write the adopted branch (and optionally the tip) to ``storage``.

    Returns the number of blocks written. Snapshots are taken every
    ``snapshot_interval`` blocks.
    """
    storage.bind_network(chain.params)
    written = 0
    for stored in chain.main_chain():
        storage.save_block(stored, tip=False)
        if stored.height % chain.params.snapshot_interval == 0:
            storage.save_snapshot(stored)
        written += 1
    target = tip or chain.tip
    storage.save_block(target, tip=True)
    return written


def restore_chain(
    params: NetworkParams,
    genesis_block: Block,
    genesis_state: State,
    storage: Storage,
) -> Chain:
    """Rebuild a chain from ``storage`` by replaying blocks through validation.

    The latest snapshot is loaded first as a cache, then checked: the replayed
    state's root must match the snapshot's. A mismatch means the cache is stale
    or corrupt and the snapshot is discarded rather than trusted.
    """
    storage.bind_network(params)
    chain = Chain(params=params, genesis_block=genesis_block, genesis_state=genesis_state)
    snapshot = storage.latest_snapshot(params)
    for block in storage.iter_blocks():
        chain.add_block(block)
    if snapshot is not None:
        replayed = chain.get(snapshot.block_hash)
        if replayed is None:
            raise StorageError(f"snapshot at height {snapshot.height} is not on a known branch")
        if replayed.state.state_root() != snapshot.state.state_root():
            raise StorageError(
                f"snapshot at height {snapshot.height} disagrees with the replayed state"
            )
    stored_tip = storage.tip_hash()
    if stored_tip is not None and stored_tip != chain.tip_hash:
        raise StorageError(
            f"stored tip {stored_tip} is not the heaviest block ({chain.tip_hash}); "
            "the database is inconsistent"
        )
    return chain


__all__ = ["SCHEMA_VERSION", "Storage", "persist_chain", "restore_chain"]
