"""The extra testnet miners the traffic generator starts and stops.

``docker-compose.yml`` runs four fixed miners (two per variant); the churn is
disabled there but remains available for larger hosts.
``synthetic_miner_creation`` adds a deterministic pool on top: it writes a keyfile
and an availability secret, registers the account, and runs a real
``sccp miner run`` child that the traffic generator later stops. These tests pin
that contract without forking a child, hashing an engine binary or paying the
production scrypt cost.
"""

from __future__ import annotations

import json
import random
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

import httpx
import pytest
from deploy import synthetic_miner_creation as miners

from sccp.chain.state import State
from sccp.chain.tx import validate_transaction
from sccp.config import TESTNET, coins_to_units
from sccp.crypto.address import validate_address
from sccp.crypto.wallet import FAST_KDF, create_wallet, read_header
from sccp.protocol.models import Transaction, TxType

NODE = "http://node"
ENGINE_DIGEST = "ab" * 32
"""What the injected :func:`engine_fingerprint` returns for any path."""

DEAD_PID = 0x7FFFFFFF
"""A pid no kernel allocates, so ``getpgid`` fails instead of signalling anything."""


class _FakeProcess:
    """A child that never forks; ``poll`` reports the exit code it was given."""

    def __init__(self) -> None:
        self.pid = DEAD_PID
        self.code: int | None = None
        self.terminated = False

    def poll(self) -> int | None:
        return self.code

    def terminate(self) -> None:
        self.terminated = True
        self.code = 0

    def kill(self) -> None:
        self.code = -9

    def wait(self, timeout: float | None = None) -> int:
        return self.code or 0


@dataclass
class _FleetFactory:
    """Build fleets on a fake host, tracking what they spawn."""

    root: Path
    clients: list[httpx.Client] = field(default_factory=list)
    commands: list[Sequence[str]] = field(default_factory=list)
    processes: list[_FakeProcess] = field(default_factory=list)

    def __call__(
        self,
        transport: httpx.MockTransport,
        *,
        allowed: int = 2,
        pool_size: int = 2,
        max_online: int = 2,
        wait_seconds: float = 0.0,
    ) -> miners.ExtraMinerFleet:
        engine = self.root / "stockfish"
        engine.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        client = httpx.Client(transport=transport, base_url=NODE)
        self.clients.append(client)
        return miners.ExtraMinerFleet(
            client=client,
            directory=self.root / "miners",
            params=TESTNET,
            engine_path=engine,
            pool_size=pool_size,
            max_online=max_online,
            wait_seconds=wait_seconds,
            poll_seconds=0.0,
            capacity=miners.HostCapacity(
                cpu_count=8,
                available_memory_mb=4096,
                allowed=allowed,
                reason="a test budget",
            ),
            spawn=self._spawn,
        )

    def _spawn(self, command: Sequence[str], log_path: Path) -> miners.ChildProcess:
        self.commands.append(tuple(command))
        process = _FakeProcess()
        self.processes.append(process)
        return process


class _Node:
    """The slice of the node's HTTP API one registration touches."""

    def __init__(
        self,
        *,
        balance: int | None = None,
        registered: bool = True,
        conflicts: int = 0,
        mined: int | None = 7,
        nonce: int = 0,
        tip: int = 20,
    ) -> None:
        self.balance = coins_to_units(1_000) if balance is None else balance
        self.registered = registered
        self.conflicts = conflicts
        self.mined = mined
        self.nonce = nonce
        self.tip = tip
        self.submitted: list[Transaction] = []
        self.attempts: list[int] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/blocks":
            return httpx.Response(200, json={"tip_height": self.tip})
        if path.startswith("/v1/accounts/"):
            return httpx.Response(200, json=self._account(path.rsplit("/", 1)[-1]))
        if path == "/v1/tx":
            return self._submit(Transaction.model_validate(json.loads(request.content)))
        if path.startswith("/v1/transactions/"):
            if self.mined is None:
                return httpx.Response(404, json={"error": "unknown transaction"})
            digest = path.rsplit("/", 1)[-1]
            return httpx.Response(
                200,
                json={
                    "network": TESTNET.network_id,
                    "txid": digest,
                    "type": "register",
                    "height": self.mined,
                },
            )
        raise AssertionError(f"unexpected request {request.url}")

    def _account(self, address: str) -> dict[str, object]:
        view: dict[str, object] = {
            "network": TESTNET.network_id,
            "address": address,
            "balance": self.balance,
            "immature": 0,
            "bonded": 0,
            "total": self.balance,
            "nonce": self.nonce,
        }
        if self.registered:
            view["registered_until_epoch"] = 10_000
        return view

    def _submit(self, tx: Transaction) -> httpx.Response:
        self.attempts.append(tx.nonce)
        if self.conflicts:
            self.conflicts -= 1
            # The node numbers against the mempool projection, so the retry has to
            # use the nonce this attempt just spent.
            self.nonce = tx.nonce + 1
            detail = f"{tx.sender} already has a pending transaction with nonce {tx.nonce}"
            return httpx.Response(400, json={"detail": detail})
        self.submitted.append(tx)
        return httpx.Response(200, json={"network": TESTNET.network_id, "txid": ENGINE_DIGEST})


@pytest.fixture(autouse=True)
def _offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never hash a real engine binary and never pay the production scrypt cost."""

    monkeypatch.setattr(miners, "KEYFILE_KDF", FAST_KDF)
    monkeypatch.setattr(miners, "engine_fingerprint", lambda _path: ENGINE_DIGEST)


@pytest.fixture
def fleet(tmp_path: Path) -> _FleetFactory:
    """A fleet builder that opens no sockets to a real node and forks nothing."""
    factory = _FleetFactory(root=tmp_path)
    yield factory
    for client in factory.clients:
        client.close()


def test_the_pool_is_derived_from_published_seeds(tmp_path: Path) -> None:
    first = miners.extra_miner(0, tmp_path)
    assert first.address == miners.extra_miner(0, tmp_path).address
    assert first.keypair.public_key_hex == miners.extra_miner(0, tmp_path).keypair.public_key_hex
    assert (first.name, first.index) == ("synthetic-miner-1", 0)
    assert first.address != miners.extra_miner(1, tmp_path).address
    assert validate_address(first.address, TESTNET)
    assert first.keyfile == tmp_path / "keys" / "synthetic-miner-1.key"
    assert first.nonce_file == tmp_path / "keys" / "synthetic-miner-1.nonce"
    assert first.endpoint == "http://synthetic-miner-1:8646"
    assert len(first.nonce_secret) == 64
    assert first.nonce_secret != miners.nonce_secret(1)
    with pytest.raises(ValueError):
        miners.miner_seed(-1)


def test_the_host_budget_shares_cores_between_the_miners_and_the_node() -> None:
    roomy = miners.host_capacity(max_extra=2, cpu_count=8, memory_mb=8192)
    assert (roomy.cpu_count, roomy.available_memory_mb) == (8, 8192)
    assert roomy.allowed == 2  # (8 - 1) / 0.5 caps at the pool; memory is not the limit
    assert "8 CPU(s)" in roomy.reason

    # The deployment's e2-standard-2: one core stays with node and relay, and one
    # extra miner still fits on the remaining core.
    small = miners.host_capacity(max_extra=2, cpu_count=2, memory_mb=7600)
    assert small.allowed == 2
    assert "stay with the node and relay" in small.reason

    starved = miners.host_capacity(max_extra=2, cpu_count=1, memory_mb=8192)
    assert starved.allowed == 0  # the reserve already fills the only core

    tight = miners.host_capacity(
        max_extra=2,
        cpu_count=8,
        memory_mb=miners.RESERVED_MEMORY_MB + 300,
    )
    assert tight.allowed == 1  # 300 MiB // 256 MiB


def test_an_unmeasurable_host_does_not_cap_the_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(miners.os, "cpu_count", lambda: None)
    monkeypatch.setattr(miners, "available_memory_mb", lambda: None)
    capacity = miners.host_capacity(max_extra=2)
    assert capacity.allowed == 2
    assert "unknown" in capacity.reason


def test_materialise_writes_one_keyfile_and_one_availability_secret(tmp_path: Path) -> None:
    miner = miners.extra_miner(0, tmp_path)
    miners.materialise(miner)
    assert read_header(miner.keyfile).address == miner.address
    assert miner.nonce_file.read_text(encoding="utf-8") == miner.nonce_secret + "\n"
    assert miner.nonce_file.stat().st_mode & 0o777 == 0o600
    stamp = miner.keyfile.stat().st_mtime_ns

    miners.materialise(miner)  # a keyfile is random-salted: keep the existing one
    assert miner.keyfile.stat().st_mtime_ns == stamp


def test_a_keyfile_for_another_address_is_refused(tmp_path: Path) -> None:
    miner = miners.extra_miner(0, tmp_path)
    stray = replace(miner, keyfile=tmp_path / "stray.key")
    create_wallet(
        stray.keyfile,
        TESTNET,
        miners.DEFAULT_PASSPHRASE,
        seed=miners.miner_seed(1),
        kdf=FAST_KDF,
    )
    with pytest.raises(miners.ExtraMinerError, match="delete the miner directory"):
        miners.materialise(stray)


def test_the_registration_is_a_transaction_the_chain_accepts(tmp_path: Path) -> None:
    miner = miners.extra_miner(0, tmp_path)
    tx = miners.registration_transaction(
        miner, nonce=0, engine=ENGINE_DIGEST, bond=TESTNET.min_bond
    )
    assert tx.type is TxType.REGISTER
    assert (tx.network, tx.sender, tx.fee) == (TESTNET.network_id, miner.address, TESTNET.min_fee)
    assert tx.payload["public_key"] == miner.keypair.public_key_hex
    assert tx.payload["endpoint"] == miner.endpoint
    assert tx.payload["engine"] == ENGINE_DIGEST
    assert tx.payload["bond"] == TESTNET.min_bond
    assert tx.signature

    state = State(params=TESTNET, height=5)
    state.ensure(miner.address).balance = TESTNET.min_bond + TESTNET.min_fee
    validate_transaction(tx, TESTNET, state, height=6)


def test_the_miner_command_matches_the_compose_miners(tmp_path: Path) -> None:
    miner = miners.extra_miner(0, tmp_path)
    command = miners.miner_command(
        miner,
        passphrase_file=tmp_path / "passphrase",
        relay="http://relay:8646",
        node=NODE,
        engine_path=Path("/usr/games/stockfish"),
        poll_seconds=0.25,
        nodes_per_move=1234,
    )
    entry = miners.sccp_command()
    passphrase_file = str(tmp_path / "passphrase")
    assert command[: len(entry)] == entry
    assert command[len(entry) : len(entry) + 2] == ["--passphrase-file", passphrase_file]
    at = command.index("miner")
    assert command[at : at + 2] == ["miner", "run"]
    tail = " ".join(command)
    assert f"--network {TESTNET.network_id}" in tail
    assert f"--key {miner.keyfile}" in tail
    assert f"--nonce-secret-file {miner.nonce_file}" in tail
    assert "--engine-profile stockfish-19" in tail
    assert "--engine-path /usr/games/stockfish" in tail
    assert "--nodes-per-move 1234" in tail
    assert f"--resign-after-plies {miners.RESIGN_AFTER_PLIES}" in tail
    assert "--quality-summary" not in tail
    assert "--relay http://relay:8646" in tail
    assert f"--node {NODE}" in tail
    assert "--poll-seconds 0.25" in tail


def test_a_start_registers_and_runs_the_miner(fleet: _FleetFactory) -> None:
    node = _Node(registered=False)
    extra = fleet(node.transport())
    miner = extra.pool[0]

    extra.start(miner)

    assert extra.online == (miner,)
    assert len(fleet.commands) == 1
    command = fleet.commands[0]
    at = command.index("miner")
    assert command[at : at + 2] == ("miner", "run")
    assert str(miner.keyfile) in command
    assert read_header(miner.keyfile).address == miner.address
    passphrase = extra.directory / "passphrase"
    assert passphrase.read_text(encoding="utf-8").strip() == miners.DEFAULT_PASSPHRASE
    assert passphrase.stat().st_mode & 0o777 == 0o600

    assert len(node.submitted) == 1
    tx = node.submitted[0]
    assert tx.type is TxType.REGISTER
    assert (tx.sender, tx.nonce) == (miner.address, 0)
    assert tx.payload["engine"] == ENGINE_DIGEST
    assert tx.payload["bond"] == TESTNET.min_bond
    assert tx.payload["endpoint"] == miner.endpoint


def test_a_registered_miner_starts_without_another_registration(fleet: _FleetFactory) -> None:
    node = _Node(registered=True)
    extra = fleet(node.transport())

    extra.start(extra.pool[0])

    assert node.attempts == []
    assert len(fleet.commands) == 1


def test_a_short_balance_without_a_grant_is_refused(fleet: _FleetFactory) -> None:
    node = _Node(balance=coins_to_units(5), registered=False)
    extra = fleet(node.transport())

    with pytest.raises(miners.ExtraMinerError, match="no grant callback was supplied"):
        extra.start(extra.pool[0])
    assert fleet.commands == []


def test_a_grant_funds_the_registration(fleet: _FleetFactory) -> None:
    node = _Node(balance=0, registered=False)
    extra = fleet(node.transport())
    miner = extra.pool[0]
    granted: list[tuple[str, int]] = []

    def grant(address: str, coins: int) -> tuple[str, bool]:
        granted.append((address, coins))
        node.balance += coins_to_units(coins)
        return ENGINE_DIGEST, False

    extra.start(miner, grant=grant)

    assert granted == [(miner.address, miners.GRANT_COINS)]
    assert [tx.sender for tx in node.submitted] == [miner.address]
    assert extra.online == (miner,)


def test_the_pool_carries_a_deliberately_weak_engine() -> None:
    """A bad engine is visible in the scoreboard, not in an attached summary."""
    weak = miners.miner_line_up(miners.WEAK_INDEX)
    assert (weak.nodes_per_move, weak.label) == (miners.WEAK_NODES_PER_MOVE, "weak")
    assert weak.nodes_per_move < miners.NODES_PER_MOVE

    honest = miners.miner_line_up(1)
    assert (honest.nodes_per_move, honest.label) == (miners.NODES_PER_MOVE, "honest")
    assert miners.miner_line_up(7) == honest


def test_the_weak_miner_is_seated_with_its_own_search_budget(tmp_path: Path) -> None:
    miner = miners.extra_miner(miners.WEAK_INDEX, tmp_path)
    line_up = miners.miner_line_up(miner.index)
    command = miners.miner_command(
        miner,
        passphrase_file=tmp_path / "passphrase",
        relay="http://relay:8646",
        node=NODE,
        engine_path=Path("/usr/games/stockfish"),
        nodes_per_move=line_up.nodes_per_move,
    )
    tail = " ".join(command)
    assert "--quality-summary" not in tail
    assert f"--nodes-per-move {miners.WEAK_NODES_PER_MOVE}" in tail


def test_a_weak_search_budget_is_not_a_whole_core(fleet: _FleetFactory) -> None:
    """The weak miner's 16-node budget is why one extra miner fits a two-core host."""
    assert miners.WEAK_NODES_PER_MOVE < miners.NODES_PER_MOVE
    assert miners.MAX_ONLINE_AT_ONCE == 1


def test_a_grant_that_never_confirms_stops_the_start(fleet: _FleetFactory) -> None:
    node = _Node(balance=0, registered=False, mined=None)
    extra = fleet(node.transport())

    with pytest.raises(miners.ExtraMinerError, match="never confirmed"):
        extra.start(extra.pool[0], grant=lambda _address, _coins: (ENGINE_DIGEST, True))
    assert fleet.commands == []


def test_a_pending_registration_nonce_is_renumbered(fleet: _FleetFactory) -> None:
    node = _Node(registered=False, conflicts=1)
    extra = fleet(node.transport(), wait_seconds=5.0)

    extra.start(extra.pool[0])

    assert node.attempts == [0, 1]
    assert [tx.nonce for tx in node.submitted] == [1]


def test_a_nonce_that_never_clears_gives_up(fleet: _FleetFactory) -> None:
    node = _Node(registered=False, conflicts=99)
    extra = fleet(node.transport())

    with pytest.raises(miners.NonceConflict):
        extra.start(extra.pool[0])
    assert fleet.commands == []


def test_the_churn_fills_then_empties_the_pool(fleet: _FleetFactory) -> None:
    extra = fleet(_Node().transport())

    came_online = extra.churn(random.Random(1))
    assert 1 <= len(came_online.started) <= 2
    assert not came_online.failed
    assert set(came_online.online) == set(came_online.started)
    assert len(extra.online) == len(came_online.started)

    went_offline = extra.churn(random.Random(2))
    assert went_offline.stopped
    assert not went_offline.started
    assert not went_offline.failed
    assert len(extra.online) == len(came_online.online) - 1
    assert went_offline.as_line().startswith("miners: stopped ")


def test_a_host_without_spare_cores_starts_nothing(fleet: _FleetFactory) -> None:
    extra = fleet(_Node().transport(), allowed=0)

    report = extra.churn(random.Random(1))

    assert report.idle
    assert report.reason == "a test budget"
    assert extra.online_limit == 0
    assert fleet.commands == []
    assert report.as_line().endswith("(a test budget)")


def test_a_failed_start_is_reported_rather_than_raised(fleet: _FleetFactory) -> None:
    node = _Node(balance=coins_to_units(5), registered=False)
    extra = fleet(node.transport(), max_online=1)

    report = extra.churn(random.Random(0))

    assert report.failed and "no grant callback was supplied" in report.failed[0]
    assert extra.online == ()
    assert report.as_line().startswith("miners: failed: ")


def test_a_child_that_exited_is_reported_and_dropped(fleet: _FleetFactory) -> None:
    extra = fleet(_Node().transport())
    extra.churn(random.Random(1))
    fleet.processes[0].code = 3

    report = extra.churn(random.Random(3))

    assert any("exited with code 3" in failure for failure in report.failed)


def test_close_stops_every_child(fleet: _FleetFactory) -> None:
    extra = fleet(_Node().transport())
    extra.churn(random.Random(1))
    assert extra.online

    extra.close()

    assert extra.online == ()
    assert all(process.terminated for process in fleet.processes)


def test_the_constructor_refuses_unusable_settings(fleet: _FleetFactory) -> None:
    with pytest.raises(miners.ExtraMinerError, match="cannot be negative"):
        fleet(_Node().transport(), pool_size=-1)
    with pytest.raises(miners.ExtraMinerError, match="at least one"):
        fleet(_Node().transport(), max_online=0)


def test_a_missing_engine_binary_is_one_clear_error(fleet: _FleetFactory, tmp_path: Path) -> None:
    extra = fleet(_Node().transport())
    extra.engine_path = tmp_path / "missing-stockfish"

    with pytest.raises(miners.ExtraMinerError, match="no engine binary"):
        extra.start(extra.pool[0])
    assert fleet.commands == []
