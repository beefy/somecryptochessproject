"""Keep a pool of extra testnet miners with fixed roles.

The compose stack runs the four miners ``init_testnet_stack.py`` creates (two
standard, two Chess960). On top of them this module seats a small, deterministic
pool of *extra* miners that play the two roles the fixed four do not: a ``slow``
seat that spends its own clock so a match is really flagged (the evidence a
bonded ``timeout_claim`` needs), and a ``gnuchess`` seat so the roster is not six
copies of one engine.

Everything is derived from published seeds, exactly like the traffic wallets and
the four stack miners: these are testnet identities with no value. A miner is not
simulated: :class:`ExtraMinerFleet` materialises a keyfile and an availability
secret, registers the account on chain and then runs a real ``sccp miner run``
child process that plays the games the relay assigns.

Two prerequisites are handled here, because a miner that starts without them is
invisible to the relay:

* the account needs ``min_bond`` plus fees before it can register, paid through a
  caller-supplied ``grant`` callable -- the traffic generator already owns the
  nonce-safe faucet path, so this module does not sign transfers of its own;
* registration only lasts ``registration_epochs``. A miner that comes back after
  a pause is re-registered with another ``register`` (the same transaction the
  bootstrap uses) before it is started, so it can commit availability again.

Seating has a budget: every miner is an engine process next to the node, the
relay, the four fixed miners, the witnesses and the traffic generator itself.
:func:`host_capacity` derives how many fit from the host's cores and free memory,
and the fleet never exceeds it. The two roles above are permanent parts of the
testnet rather than a churn demo, so the traffic generator keeps the pool seated
(``--no-extra-miner-churn``) and the fleet repairs a child that exits; the
original churn is still available with ``--extra-miner-churn``. A host that
cannot be measured is not capped; one that cannot fit a miner logs why and seats
none.
"""

from __future__ import annotations

import hashlib
import os
import random
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Protocol

import httpx

from sccp.chain.state import epoch_of
from sccp.chain.tx import sign_transaction, txid
from sccp.chess.engines import engine_fingerprint
from sccp.config import TESTNET, NetworkParams, coins_to_units
from sccp.crypto.address import encode_address
from sccp.crypto.pq import HybridKeypair, hybrid_keypair_from_seed
from sccp.crypto.wallet import KdfParams, create_wallet, read_header
from sccp.protocol.models import Transaction, TxType

FIXED_MINERS: Final = 2
"""The miners ``docker-compose.yml`` runs itself; churn happens beside them."""

MAX_EXTRA_MINERS: Final = 2
"""How many extra miners the pool defines."""

MAX_ONLINE_AT_ONCE: Final = 1
"""A churn seats one extra miner at a time, so the pool can keep swapping them."""

CHURN_PROBABILITY: Final = 0.5
"""Chance that a partial churn seats another miner instead of stopping one."""

SEED_NAMESPACE: Final = b"sccp/testnet/extra-miner/"
DEFAULT_PASSPHRASE: Final = "sccp-testnet-extra-miners"
"""The published passphrase for the extra miners' keyfiles, like the stack's."""

KEYFILE_KDF: Final = KdfParams(n=2**14)
"""Cheap scrypt parameters: a container has to open these keyfiles quickly."""

GRANT_COINS: Final = 100
"""Coins per top-up. Covers nine registrations (``min_bond`` = 10 coins each)."""

MIN_BALANCE_COINS: Final = 20
"""Top an account up when it holds less than this, so it can always register."""

CORES_PER_MINER: Final = 0.5
"""Share of a core one extra miner budgets.

The stack plays at 5 000 nodes per move, so a move is a few milliseconds of one
thread and the process sleeps between polls. Charging a whole core per miner
would keep the churn inert on every host smaller than six vCPUs; half a core
still bounds two miners' overlapping searches to one core.
"""

RESERVED_CORES: Final = 1
"""Cores kept for the node, the relay and the witnesses, which must answer while
a miner searches."""

PER_MINER_MEMORY_MB: Final = 256
RESERVED_MEMORY_MB: Final = 1024

WAIT_SECONDS: Final = 900.0
"""How long to wait for the grant and registration blocks. One block is a minute."""

POLL_SECONDS: Final = 5.0
NODES_PER_MOVE: Final = 5_000
"""The fixed miners' search budget, so extra miners play at the same strength."""

MINER_POLL_SECONDS: Final = 0.5
"""How often an extra miner asks the relay for work.

The fixed miners use 0.1s. Six seats polling a relay that answers in
milliseconds is the binding CPU cost on a two-vCPU host, so the extra seats pick
their work up half a second later instead of competing with the four that cap
the round.
"""

RESIGN_AFTER_PLIES: Final = 60
"""Resign on our own turn after this many plies, as the fixed miners do."""

SLOW_INDEX: Final = 0
"""The pool member that deliberately loses on time.

Every honest miner finishes a 30+0 game long before a clock runs out, so the
flag receipt, the bonded ``timeout_claim`` and the witness checkpoint they
depend on are never produced by normal play. This seat waits
:data:`SLOW_MOVE_DELAY_SECONDS` before searching on every one of its turns: the
relay charges that wait to its own clock, and after about seventeen of its moves
the clock is gone. It resigns nothing -- resigning would end the game before the
flag -- so the opponent gets exactly the timeout the protocol has to settle.
"""

SLOW_MOVE_DELAY_SECONDS: Final = 110.0
"""Long enough to spend 30 minutes of clock in about seventeen own moves."""

ALTERNATE_INDEX: Final = 1
"""The pool member that runs a different engine from the rest of the roster.

Six copies of one binary prove very little about engine diversity. This seat
plays GNU Chess, which ships in Debian beside Stockfish and speaks UCI once it is
told to.
"""

ALTERNATE_ENGINE: Final = "gnuchess"
ALTERNATE_ENGINE_PATH: Final = Path("/usr/games/gnuchess")
"""Where Debian's ``gnuchess`` package installs the binary in the image."""

ALTERNATE_DEPTH: Final = 5
"""GNU Chess' search budget.

It ignores ``go nodes`` (it searches until told to stop, so a node limit never
returns a move) and honours ``go depth``; depth 5 is a few thousand nodes -- a
real opponent, not a random mover.
"""

NONCE_CONFLICT: Final = "already has a pending transaction with nonce"
"""The node's refusal when the next nonce of an account is already in the pool."""


@dataclass(frozen=True, slots=True)
class MinerLineUp:
    """How one pool member is told to play."""

    label: str
    """A short name for the log line: ``slow`` or ``gnuchess``."""

    engine_profile: str = "stockfish-19"
    engine_path: Path = Path("/usr/games/stockfish")
    nodes_per_move: int = NODES_PER_MOVE
    depth: int | None = None
    """A fixed depth instead of a node limit, for engines without one."""
    move_delay_seconds: float = 0.0
    resign_after_plies: int | None = RESIGN_AFTER_PLIES


def miner_line_up(index: int) -> MinerLineUp:
    """The role of pool member ``index``.

    Two roles beyond the honest default, and no others: one seat is deliberately
    slow so its clock runs out (the only way a timeout claim gets its flag), and
    one runs a different engine so the roster is not a monoculture. Extra members
    beyond the pool play like the fixed miners.
    """
    if index == SLOW_INDEX:
        return MinerLineUp(
            label="slow",
            move_delay_seconds=SLOW_MOVE_DELAY_SECONDS,
            resign_after_plies=None,
        )
    if index == ALTERNATE_INDEX:
        return MinerLineUp(
            label=ALTERNATE_ENGINE,
            engine_profile=ALTERNATE_ENGINE,
            engine_path=ALTERNATE_ENGINE_PATH,
            depth=ALTERNATE_DEPTH,
        )
    return MinerLineUp(label="honest")


class ExtraMinerError(RuntimeError):
    """An extra miner could not be materialised, registered or started."""


class NonceConflict(ExtraMinerError):
    """The registration nonce is already pending; the next block frees it."""


Grant = Callable[[str, int], tuple[str, bool]]
"""``(address, coins) -> (txid, went through the faucet endpoint)``."""


class ChildProcess(Protocol):
    """The slice of :class:`subprocess.Popen` the fleet needs; injectable in tests."""

    pid: int

    def poll(self) -> int | None:
        """The exit code, or ``None`` while the process runs."""

    def terminate(self) -> None:
        """Ask the process to stop."""

    def kill(self) -> None:
        """Kill the process."""

    def wait(self, timeout: float | None = None) -> int:
        """Block until the process exits."""


Spawn = Callable[[Sequence[str], Path], ChildProcess]
"""``(command, log path) -> running process``, so tests never fork a miner."""


@dataclass(frozen=True, slots=True)
class ExtraMiner:
    """One deterministic extra miner and the files it runs from."""

    index: int
    name: str
    keypair: HybridKeypair
    address: str
    nonce_secret: str
    keyfile: Path
    nonce_file: Path
    endpoint: str


@dataclass(frozen=True, slots=True)
class HostCapacity:
    """How many extra miners the host can take, and the reason for the answer."""

    cpu_count: int | None
    available_memory_mb: int | None
    allowed: int
    reason: str


def miner_seed(index: int) -> bytes:
    """The fixed, published seed for extra miner ``index``."""
    if index < 0:
        raise ValueError("a miner index must be non-negative")
    return hashlib.sha256(SEED_NAMESPACE + index.to_bytes(4, "big")).digest()


def nonce_secret(index: int) -> str:
    """The fixed 32-byte availability secret for extra miner ``index``."""
    return hashlib.sha256(SEED_NAMESPACE + b"nonce/" + index.to_bytes(4, "big")).hexdigest()


def extra_miner(
    index: int,
    directory: str | Path,
    *,
    params: NetworkParams = TESTNET,
    endpoint: str | None = None,
) -> ExtraMiner:
    """Derive extra miner ``index``; the same index always yields the same key."""
    keypair = hybrid_keypair_from_seed(miner_seed(index))
    name = f"synthetic-miner-{index + 1}"
    root = Path(directory)
    return ExtraMiner(
        index=index,
        name=name,
        keypair=keypair,
        address=encode_address(keypair.public_key_bytes, params),
        nonce_secret=nonce_secret(index),
        keyfile=root / "keys" / f"{name}.key",
        nonce_file=root / "keys" / f"{name}.nonce",
        endpoint=endpoint or f"http://{name}:8646",
    )


# -- host budget --------------------------------------------------------------


def _cgroup_memory_mb() -> int | None:
    """The container's memory limit in MiB, when one is set."""
    for path in (
        Path("/sys/fs/cgroup/memory.max"),
        Path("/sys/fs/cgroup/memory/memory.limit_in_bytes"),
    ):
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if text.isdigit() and 0 < int(text) < 1 << 60:
            return int(text) // (1024 * 1024)
    return None


def available_memory_mb() -> int | None:
    """Free memory in MiB, capped by the container limit, or ``None`` if unknown."""
    free: int | None = None
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemAvailable:"):
                free = int(line.split()[1]) // 1024
                break
    except (OSError, ValueError):
        free = None
    if free is None:
        try:
            size = int(os.sysconf("SC_PAGE_SIZE")) * int(os.sysconf("SC_AVPHYS_PAGES"))
        except (AttributeError, OSError, ValueError):
            return None
        free = size // (1024 * 1024)
    limit = _cgroup_memory_mb()
    return free if limit is None else min(free, limit)


def host_capacity(
    *,
    max_extra: int = MAX_EXTRA_MINERS,
    cpu_count: int | None = None,
    memory_mb: int | None = None,
) -> HostCapacity:
    """How many extra miners this host can take before the fixed ones starve.

    ``cpu_count`` and ``memory_mb`` are injectable so the arithmetic is testable;
    when they are omitted the host is measured.

    An unmeasurable dimension does not cap the result: refusing to start on an
    unknown platform would be worse than trying, and the caller can always pass
    ``--max-extra-miners 0``.
    """
    cpus = os.cpu_count() if cpu_count is None else cpu_count
    memory = available_memory_mb() if memory_mb is None else memory_mb
    by_cpu = (
        max_extra if cpus is None else max(0, int((cpus - RESERVED_CORES) / CORES_PER_MINER + 1e-9))
    )
    by_memory = (
        max_extra
        if memory is None
        else max(0, (memory - RESERVED_MEMORY_MB) // PER_MINER_MEMORY_MB)
    )
    allowed = max(0, min(max_extra, by_cpu, by_memory))
    cores = "unknown" if cpus is None else str(cpus)
    free = "unknown" if memory is None else f"{memory} MiB"
    reason = (
        f"the host has {cores} CPU(s) and {free} free; {RESERVED_CORES:g} core(s) "
        f"stay with the node and relay, a miner budgets {CORES_PER_MINER:g}, "
        f"leaving room for {allowed}"
    )
    return HostCapacity(cpu_count=cpus, available_memory_mb=memory, allowed=allowed, reason=reason)


# -- materialising a miner ----------------------------------------------------


def materialise(
    miner: ExtraMiner,
    *,
    params: NetworkParams = TESTNET,
    passphrase: str = DEFAULT_PASSPHRASE,
) -> None:
    """Write the miner's keyfile and availability secret, once.

    A keyfile is random-salted, so an existing one is kept; it is only read back to
    prove it still holds the deterministic address this miner is registered under.
    Replacing it would silently seat a different account than the chain knows.
    """
    miner.keyfile.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if miner.keyfile.is_file():
        existing = read_header(miner.keyfile).address
        if existing != miner.address:
            raise ExtraMinerError(
                f"{miner.keyfile} holds {existing}, expected {miner.address}; "
                "delete the miner directory to reseat the pool"
            )
    else:
        create_wallet(
            miner.keyfile,
            params,
            passphrase,
            seed=miner_seed(miner.index),
            kdf=KEYFILE_KDF,
        )
    if not miner.nonce_file.is_file():
        miner.nonce_file.write_text(miner.nonce_secret + "\n", encoding="utf-8")
        miner.nonce_file.chmod(0o600)


def registration_transaction(
    miner: ExtraMiner,
    *,
    nonce: int,
    engine: str,
    bond: int,
    params: NetworkParams = TESTNET,
) -> Transaction:
    """Build and sign the registration a miner submits; the node still validates it."""
    tx = Transaction(
        network=params.network_id,
        type=TxType.REGISTER,
        sender=miner.address,
        nonce=nonce,
        fee=params.min_fee,
        payload={
            "public_key": miner.keypair.public_key_hex,
            "endpoint": miner.endpoint,
            "engine": engine,
            "bond": bond,
        },
    )
    return sign_transaction(miner.keypair, tx)


def sccp_command() -> list[str]:
    """The ``sccp`` entry point, from ``PATH`` or from this interpreter."""
    executable = shutil.which("sccp")
    if executable is not None:
        return [executable]
    return [sys.executable, "-m", "sccp.cli"]


def miner_command(
    miner: ExtraMiner,
    *,
    passphrase_file: Path,
    relay: str,
    node: str,
    line_up: MinerLineUp,
    poll_seconds: float = MINER_POLL_SECONDS,
    witness_key_files: Sequence[Path] = (),
    witness_urls: Sequence[str] = (),
    params: NetworkParams = TESTNET,
) -> list[str]:
    """The ``sccp miner run`` command line for ``miner``.

    The arguments mirror the compose file's miners -- same network, same relay
    and node, same resign policy -- plus this module's keyfile and availability
    secret so the miner commits and reveals on its own every epoch. What differs
    per seat comes from :func:`miner_line_up`: the search budget (nodes, or a
    depth for an engine that ignores node limits), an optional move delay, and
    the engine itself.

    ``witness_key_file`` pins the clock witnesses the seat trusts, without which
    a flagged match cannot become a timeout claim; ``witness_urls`` let the seat
    ask those witnesses for evidence when the relay has none.
    """
    command = [
        *sccp_command(),
        "--passphrase-file",
        str(passphrase_file),
        "miner",
        "run",
        "--network",
        params.network_id,
        "--key",
        str(miner.keyfile),
        "--nonce-secret-file",
        str(miner.nonce_file),
        "--engine-profile",
        line_up.engine_profile,
        "--engine-path",
        str(line_up.engine_path),
    ]
    if line_up.depth is not None:
        command += ["--depth", str(line_up.depth)]
    else:
        command += ["--nodes-per-move", str(line_up.nodes_per_move)]
    if line_up.resign_after_plies is not None:
        command += ["--resign-after-plies", str(line_up.resign_after_plies)]
    if line_up.move_delay_seconds > 0:
        command += ["--move-delay-seconds", str(line_up.move_delay_seconds)]
    for path in witness_key_files:
        command += ["--relay-witness-key-file", str(path)]
    for url in witness_urls:
        command += ["--witness-url", url]
    return [
        *command,
        "--relay",
        relay,
        "--node",
        node,
        "--poll-seconds",
        str(poll_seconds),
    ]


# -- talking to the node ------------------------------------------------------


def _get_object(client: httpx.Client, path: str) -> Mapping[str, object]:
    """GET ``path`` and return a JSON object, or raise :class:`ExtraMinerError`."""
    response = client.get(path)
    if response.status_code >= 300:
        raise ExtraMinerError(f"GET {path} failed: HTTP {response.status_code} {response.text}")
    payload = response.json()
    if not isinstance(payload, Mapping):
        raise ExtraMinerError(f"GET {path} did not return an object")
    return payload


def _integer(value: object, field_name: str, context: str) -> int:
    """Return ``value`` as a non-negative integer, or raise :class:`ExtraMinerError`."""
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ExtraMinerError(f"{context} reported an invalid {field_name}: {value!r}")
    return value


def account_view(client: httpx.Client, address: str) -> Mapping[str, object]:
    """The committed account view for ``address``."""
    return _get_object(client, f"/v1/accounts/{address}")


def tip_height(client: httpx.Client) -> int:
    """The node's current tip height."""
    payload = _get_object(client, "/v1/blocks?limit=1")
    return _integer(payload.get("tip_height"), "tip_height", "the node")


def _transaction_height(client: httpx.Client, digest: str) -> int | None:
    """The block height holding ``digest``; ``-1`` while pending, ``None`` if unknown."""
    response = client.get(f"/v1/transactions/{digest}")
    if response.status_code == 404:
        return None
    if response.status_code >= 300:
        raise ExtraMinerError(
            f"GET /v1/transactions/{digest} failed: HTTP {response.status_code} {response.text}"
        )
    payload = response.json()
    height = payload.get("height") if isinstance(payload, Mapping) else None
    if isinstance(height, int) and not isinstance(height, bool) and height >= 0:
        return height
    return -1


def wait_for_transaction(
    client: httpx.Client,
    digest: str,
    *,
    wait_seconds: float = WAIT_SECONDS,
    poll_seconds: float = POLL_SECONDS,
) -> bool:
    """Wait until ``digest`` is in a block; ``False`` if it is dropped or times out."""
    deadline = time.monotonic() + wait_seconds
    while True:
        height = _transaction_height(client, digest)
        if height is not None and height >= 0:
            return True
        if height is None:
            return False
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll_seconds)


def submit_transaction(client: httpx.Client, tx: Transaction) -> str:
    """Submit one signed transaction; return its txid.

    A nonce the pool already holds is reported as :class:`NonceConflict` so the
    caller can wait for the block that frees it instead of failing the churn.
    """
    response = client.post("/v1/tx", json=tx.model_dump(mode="json"))
    if response.status_code >= 300:
        detail = response.text
        if response.status_code == 400 and NONCE_CONFLICT in detail:
            raise NonceConflict(f"nonce {tx.nonce} is already pending: {detail}")
        raise ExtraMinerError(
            f"the node refused the registration: HTTP {response.status_code} {detail}"
        )
    payload = response.json()
    digest = payload.get("txid") if isinstance(payload, Mapping) else None
    return digest if isinstance(digest, str) and digest else txid(tx)


# -- the fleet ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ChurnReport:
    """What one churn did, for the log line and for tests."""

    started: tuple[str, ...] = ()
    stopped: tuple[str, ...] = ()
    online: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()
    reason: str = ""

    @property
    def idle(self) -> bool:
        """Whether the churn changed nothing."""
        return not (self.started or self.stopped or self.failed)

    def as_line(self) -> str:
        """One human-readable line describing the churn."""
        if self.failed:
            head = "failed: " + "; ".join(self.failed)
        elif self.started:
            head = "started " + ", ".join(self.started)
        elif self.stopped:
            head = "stopped " + ", ".join(self.stopped)
        else:
            head = "no change"
        online = ", ".join(self.online) if self.online else "none"
        return f"miners: {head}; online: {online}" + (f" ({self.reason})" if self.reason else "")


@dataclass
class RunningMiner:
    """One ``sccp miner run`` child of the traffic container."""

    miner: ExtraMiner
    process: ChildProcess
    log_path: Path


def _spawn(command: Sequence[str], log_path: Path) -> ChildProcess:
    """Start ``command`` in its own process group, logging to ``log_path``."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as log:
        return subprocess.Popen(
            list(command),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )


def _terminate(process: ChildProcess, *, timeout: float = 10.0) -> None:
    """Stop a miner and the engine it spawned, then stop waiting nicely."""
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGTERM)
    except OSError:
        process.terminate()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


@dataclass
class ExtraMinerFleet:
    """Start and stop deterministic extra miners within the host's capacity.

    The pool is materialised lazily: a keyfile is only written when its miner is
    first started, so a deployment that cannot host an extra miner writes nothing.
    ``grant`` funds an account through whatever path the caller owns; without it a
    miner whose balance is short is refused with a clear message instead of being
    started into a registration the node will reject.
    """

    client: httpx.Client
    directory: Path
    params: NetworkParams = TESTNET
    relay: str = "http://relay:8646"
    node: str = "http://node:8645"
    pool_size: int = MAX_EXTRA_MINERS
    max_online: int = MAX_ONLINE_AT_ONCE
    passphrase: str = DEFAULT_PASSPHRASE
    wait_seconds: float = WAIT_SECONDS
    poll_seconds: float = POLL_SECONDS
    miner_poll_seconds: float = MINER_POLL_SECONDS
    witness_key_files: tuple[Path, ...] = ()
    """The clock witnesses' public keys, as ``miner run`` reads them.

    Without them an extra miner can play and win, but it refuses every timeout
    claim: the flag receipt cannot be checked against a key it does not have.
    """
    witness_urls: tuple[str, ...] = ()
    """Independent witnesses the seats may ask for a claim's evidence."""
    line_up: Callable[[int], MinerLineUp] = miner_line_up
    """How pool member ``index`` plays: the production roles, injectable in tests."""
    capacity: HostCapacity = field(default_factory=host_capacity)
    spawn: Spawn = _spawn
    _pool: tuple[ExtraMiner, ...] = field(init=False, default=(), repr=False)
    _running: dict[int, RunningMiner] = field(init=False, default_factory=dict, repr=False)
    _engines: dict[Path, str] = field(init=False, default_factory=dict, repr=False)
    _passphrase_file: Path = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if self.pool_size < 0:
            raise ExtraMinerError("the extra miner pool cannot be negative")
        if self.max_online < 1:
            raise ExtraMinerError("an extra miner cap must be at least one")
        self._pool = tuple(
            extra_miner(index, self.directory, params=self.params)
            for index in range(self.pool_size)
        )
        self._passphrase_file = self.directory / "passphrase"

    @property
    def pool(self) -> tuple[ExtraMiner, ...]:
        """Every extra miner this deployment defines."""
        return self._pool

    @property
    def online(self) -> tuple[ExtraMiner, ...]:
        """The extra miners currently running, in pool order."""
        return tuple(running.miner for _, running in sorted(self._running.items()))

    @property
    def online_limit(self) -> int:
        """How many extra miners may run at once."""
        return max(0, min(self.capacity.allowed, self.max_online, len(self._pool)))

    def start(self, miner: ExtraMiner, *, grant: Grant | None = None) -> None:
        """Register ``miner`` if needed, then run it against the relay."""
        if miner.index in self._running:
            return
        if len(self._running) >= self.online_limit:
            raise ExtraMinerError(
                f"{self.online_limit} extra miner(s) already run: {self.capacity.reason}"
            )
        line_up = self.line_up(miner.index)
        engine = self._engine_digest(line_up)
        self._write_passphrase()
        materialise(miner, params=self.params, passphrase=self.passphrase)
        self._register(miner, grant=grant, engine=engine)
        command = miner_command(
            miner,
            passphrase_file=self._passphrase_file,
            relay=self.relay,
            node=self.node,
            line_up=line_up,
            poll_seconds=self.miner_poll_seconds,
            witness_key_files=self.witness_key_files,
            witness_urls=self.witness_urls,
            params=self.params,
        )
        log_path = self.directory / "logs" / f"{miner.name}.log"
        process = self.spawn(command, log_path)
        self._running[miner.index] = RunningMiner(miner=miner, process=process, log_path=log_path)

    def stop(self, miner: ExtraMiner) -> None:
        """Stop ``miner`` if it is running."""
        running = self._running.pop(miner.index, None)
        if running is not None:
            _terminate(running.process)

    def reap(self) -> tuple[tuple[str, int], ...]:
        """Drop miners whose process exited; return their names and exit codes."""
        exited: list[tuple[str, int]] = []
        for index, running in list(self._running.items()):
            code = running.process.poll()
            if code is None:
                continue
            del self._running[index]
            exited.append((running.miner.name, code))
        return tuple(exited)

    def churn(self, rng: random.Random, *, grant: Grant | None = None) -> ChurnReport:
        """Start or stop a random miner, keeping one or two of the pool online.

        An empty pool starts one or two miners -- the "they came online" case. With
        miners already online the churn is a coin flip between seating one more (up
        to the online limit) and taking one down, so the roster keeps changing. A
        failure is reported rather than raised: a miner the host or the chain
        refuses must not stop the wallet traffic the generator exists for.
        """
        exited = self.reap()
        limit = self.online_limit
        if limit == 0:
            return ChurnReport(reason=self.capacity.reason)
        started: list[str] = []
        stopped: list[str] = []
        failed = [f"{name} exited with code {code}" for name, code in exited]
        try:
            if not self._running:
                for miner in rng.sample(list(self._pool), rng.randint(1, limit)):
                    self.start(miner, grant=grant)
                    started.append(miner.name)
            elif len(self._running) < limit and rng.random() < CHURN_PROBABILITY:
                idle = [item for item in self._pool if item.index not in self._running]
                miner = rng.choice(idle)
                self.start(miner, grant=grant)
                started.append(miner.name)
            else:
                running = rng.choice(list(self._running.values()))
                self.stop(running.miner)
                stopped.append(running.miner.name)
        except (ExtraMinerError, httpx.HTTPError) as exc:
            failed.append(str(exc))
        return ChurnReport(
            started=tuple(started),
            stopped=tuple(stopped),
            online=tuple(item.name for item in self.online),
            failed=tuple(failed),
        )

    def start_all(self, *, grant: Grant | None = None) -> ChurnReport:
        """Seat the whole pool and keep it seated.

        The churn demonstrates roster turnover; the deployment's fixed roles need
        the opposite. ``start_all`` reaps miners that exited, then starts every
        pool member the host budget allows and leaves it running. A crash is
        repaired on the next cycle rather than waiting for a coin flip, and the
        slow seat and the alternate engine keep their identities from round to
        round -- which is the point of having them.
        """
        exited = self.reap()
        started: list[str] = []
        failed = [f"{name} exited with code {code}" for name, code in exited]
        for miner in self._pool:
            if len(self._running) >= self.online_limit:
                break
            if miner.index in self._running:
                continue
            try:
                self.start(miner, grant=grant)
            except (ExtraMinerError, httpx.HTTPError) as exc:
                failed.append(f"{miner.name}: {exc}")
                continue
            started.append(miner.name)
        return ChurnReport(
            started=tuple(started),
            online=tuple(item.name for item in self.online),
            failed=tuple(failed),
            reason=self.capacity.reason if self.online_limit == 0 else "",
        )

    def close(self) -> None:
        """Stop every miner this fleet started."""
        for index in list(self._running):
            self.stop(self._pool[index])

    # -- registration -------------------------------------------------------

    def _engine_digest(self, line_up: MinerLineUp) -> str:
        """The engine fingerprint ``line_up``'s miner registers, computed once."""
        cached = self._engines.get(line_up.engine_path)
        if cached is not None:
            return cached
        if not line_up.engine_path.is_file():
            raise ExtraMinerError(
                f"no engine binary at {line_up.engine_path}; the {line_up.label} seat "
                "needs the engine its profile names"
            )
        digest = engine_fingerprint(line_up.engine_path)
        self._engines[line_up.engine_path] = digest
        return digest

    def _write_passphrase(self) -> None:
        """Write the published passphrase file the child's ``--passphrase-file`` reads."""
        if not self._passphrase_file.is_file():
            self._passphrase_file.parent.mkdir(parents=True, exist_ok=True)
            self._passphrase_file.write_text(self.passphrase + "\n", encoding="utf-8")
            self._passphrase_file.chmod(0o600)

    def _register(self, miner: ExtraMiner, *, grant: Grant | None, engine: str) -> None:
        """Fund, then register ``miner`` for the epochs it is about to play."""
        cost = self.params.min_bond + self.params.min_fee
        threshold = max(cost, coins_to_units(MIN_BALANCE_COINS))
        view = account_view(self.client, miner.address)
        balance = _integer(view.get("balance"), "balance", miner.address)
        if balance < threshold:
            if grant is None:
                raise ExtraMinerError(
                    f"{miner.address} holds {balance} units; it needs {cost} to register "
                    "and no grant callback was supplied"
                )
            digest, _ = grant(miner.address, GRANT_COINS)
            if not wait_for_transaction(
                self.client,
                digest,
                wait_seconds=self.wait_seconds,
                poll_seconds=self.poll_seconds,
            ):
                raise ExtraMinerError(f"the grant to {miner.address} never confirmed")
            view = account_view(self.client, miner.address)
            balance = _integer(view.get("balance"), "balance", miner.address)
            if balance < cost:
                raise ExtraMinerError(
                    f"{miner.address} holds {balance} units after a {GRANT_COINS}-coin "
                    f"grant, less than the {cost}-unit registration"
                )
        if not self._needs_registration(view):
            return
        deadline = time.monotonic() + self.wait_seconds
        nonce = _integer(view.get("nonce"), "nonce", miner.address)
        while True:
            tx = registration_transaction(
                miner,
                nonce=nonce,
                engine=engine,
                bond=self.params.min_bond,
                params=self.params,
            )
            try:
                digest = submit_transaction(self.client, tx)
            except NonceConflict:
                if time.monotonic() >= deadline:
                    raise
                print(
                    f"miners: the nonce of {miner.name} is still pending; "
                    "waiting for the block that frees it",
                    file=sys.stderr,
                )
                time.sleep(self.poll_seconds)
                view = account_view(self.client, miner.address)
                nonce = _integer(view.get("nonce"), "nonce", miner.address)
                continue
            break
        if not wait_for_transaction(
            self.client, digest, wait_seconds=self.wait_seconds, poll_seconds=self.poll_seconds
        ):
            raise ExtraMinerError(f"the registration of {miner.name} never confirmed")

    def _needs_registration(self, view: Mapping[str, object]) -> bool:
        """Whether the account's registration has lapsed or lapses before it plays.

        A miner may only commit availability ``availability_lead`` epochs ahead and
        the relay only matches registered accounts, so a registration that ends at
        the current epoch is already too short to seat this miner.
        """
        until = view.get("registered_until_epoch")
        if not isinstance(until, int) or isinstance(until, bool):
            return True
        epoch = epoch_of(tip_height(self.client), self.params)
        return until < epoch + self.params.availability_lead


if __name__ == "__main__":  # pragma: no cover - the generator is the entry point
    raise SystemExit(
        "synthetic_miner_creation is a library: run deploy/synthetic_testnet_traffic.py, "
        "which drives it (`--extra-miners`), or import ExtraMinerFleet from a script."
    )
