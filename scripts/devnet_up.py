#!/usr/bin/env python3
"""Start every process a local SCCP devnet needs, and stop them together.

One command brings up the whole multi-process devnet:

* the built web UI, served by the node at ``/`` (or, with ``--web-ui dev``, by
  the Vite dev server);
* a devnet wallet you can use with the CLI and the explorer;
* two deterministic miners from ``deploy/setup_devnet_stack.py``;
* the matchmaking relay, following the node across epochs;
* an independent clock witness;
* the full node API that owns the chain.

The pieces have to start in dependency order even though a devnet is described
as a list of services: the chain and keys come first, then the node, the relay,
the witness, and finally the miners. Every child runs in its own process group
with a log under ``<stack>/logs``; Ctrl+C shuts the group down cleanly.

Examples::

    .venv/bin/python scripts/devnet_up.py
    .venv/bin/python scripts/devnet_up.py --fresh
    .venv/bin/python scripts/devnet_up.py --web-ui dev
    .venv/bin/python scripts/devnet_up.py --web-ui none --ready-timeout 20

The script uses the same deterministic devnet stack the operator guide does, so
rerunning it without ``--fresh`` resumes the chain and keeps the keyfiles.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from typing import IO, Any
from urllib.error import URLError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
WEB_DIR = ROOT / "src" / "web"
DEFAULT_STACK = ROOT / ".devnet-stack"
DEFAULT_PASSPHRASE = "sccp-devnet-stack"
PASSPHRASE_FILE = "passphrase"
MINER_COUNT = 2
LOG_LINES = 20


class DevnetError(RuntimeError):
    """A service, keyfile or startup step could not be completed."""


@dataclass(frozen=True, slots=True)
class KeyInfo:
    """A keyfile this run created or reused."""

    name: str
    path: Path
    address: str
    public_key: str


@dataclass(frozen=True, slots=True)
class MinerInfo:
    """One deterministic miner written by the devnet stack bootstrap."""

    name: str
    address: str
    keyfile: Path
    nonce_file: Path


@dataclass(slots=True)
class Service:
    """One child process and the log/thread that drains its output."""

    name: str
    command: tuple[str, ...]
    process: subprocess.Popen[str]
    log_path: Path
    log_handle: IO[str]
    thread: threading.Thread


def _parser() -> argparse.ArgumentParser:
    """The command line documented in this module's docstring."""
    parser = argparse.ArgumentParser(
        description="Run a complete local SCCP devnet in separate processes.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--stack",
        type=Path,
        default=DEFAULT_STACK,
        help="directory for keys, chain, relay config and logs",
    )
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="rebuild the bootstrap chain (keys and the created wallet are kept)",
    )
    parser.add_argument("--host", default="127.0.0.1", help="interface every service binds")
    parser.add_argument("--node-port", type=int, default=8645, help="node API and built UI port")
    parser.add_argument("--relay-port", type=int, default=8646, help="matchmaking relay port")
    parser.add_argument("--witness-port", type=int, default=8647, help="clock witness port")
    parser.add_argument("--web-port", type=int, default=5173, help="Vite dev-server port")
    parser.add_argument(
        "--web-ui",
        choices=("built", "dev", "none"),
        default="built",
        help="serve the built UI from the node, run Vite, or start no UI",
    )
    parser.add_argument(
        "--web-dist",
        type=Path,
        default=None,
        help="built UI directory (sets SCCP_WEB_DIST; default: src/web/dist)",
    )
    parser.add_argument(
        "--no-build-web",
        action="store_true",
        help="never run npm; require an existing dist (built) or node_modules (dev)",
    )
    parser.add_argument(
        "--ready-timeout",
        type=float,
        default=30.0,
        help="seconds each HTTP service gets to answer its status route",
    )
    parser.add_argument(
        "--resign-after-plies",
        type=int,
        default=60,
        help="make miners resign drawn games after this many plies (0 disables)",
    )
    parser.add_argument(
        "--passphrase",
        default=None,
        help=(
            "keyfile passphrase (default: $SCCP_DEVNET_STACK_PASSPHRASE, "
            "the existing stack's passphrase file, or the published devnet one)"
        ),
    )
    parser.add_argument("--quiet", action="store_true", help="log child output without echoing it")
    return parser


def _validate_arguments(args: argparse.Namespace) -> None:
    """Reject values that would make one child bind the wrong interface or port."""
    ports = {
        "--node-port": args.node_port,
        "--relay-port": args.relay_port,
        "--witness-port": args.witness_port,
    }
    if args.web_ui == "dev":
        ports["--web-port"] = args.web_port
    for label, port in ports.items():
        if not 1 <= port <= 65535:
            raise DevnetError(f"{label} must be between 1 and 65535, not {port}")
    if len(set(ports.values())) != len(ports):
        raise DevnetError(f"service ports must be distinct: {ports}")
    if args.ready_timeout <= 0:
        raise DevnetError("--ready-timeout must be positive")
    if args.resign_after_plies < 0:
        raise DevnetError("--resign-after-plies must not be negative")
    if args.web_ui == "dev" and args.web_dist is not None:
        raise DevnetError("--web-dist is for --web-ui built, not the Vite dev server")


def _stack_passphrase(args: argparse.Namespace, stack: Path) -> str:
    """Pick the passphrase that unlocks an existing stack, or create a new one."""
    if args.passphrase:
        return args.passphrase
    from_env = os.environ.get("SCCP_DEVNET_STACK_PASSPHRASE")
    if from_env:
        return from_env
    existing = stack / PASSPHRASE_FILE
    if existing.is_file():
        try:
            value = existing.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise DevnetError(f"cannot read {existing}: {exc}") from exc
        if value:
            return value
    return DEFAULT_PASSPHRASE


def _environment(args: argparse.Namespace, passphrase: str) -> dict[str, str]:
    """The environment every child gets: source path, unbuffered logs, passphrase."""
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["SCCP_DEVNET_STACK_PASSPHRASE"] = passphrase
    paths = [str(ROOT / "src")]
    if env.get("PYTHONPATH"):
        paths.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(paths)
    if args.web_dist is not None:
        env["SCCP_WEB_DIST"] = str(args.web_dist.expanduser().resolve())
    return env


def _local_host(host: str) -> str:
    """The host to put in URLs: a bind-all address is not a useful client target."""
    return "127.0.0.1" if host in {"0.0.0.0", "::"} else host


def _sccp_command(
    *arguments: str,
    data_dir: Path | None = None,
    passphrase_file: Path | None = None,
) -> list[str]:
    """Build an ``sccp`` invocation with the global options before the command."""
    command = [sys.executable, "-m", "sccp.cli", "--network", "devnet"]
    if data_dir is not None:
        command.extend(["--data-dir", str(data_dir)])
    if passphrase_file is not None:
        command.extend(["--passphrase-file", str(passphrase_file)])
    command.extend(arguments)
    return command


def _run_checked(
    command: Sequence[str],
    *,
    env: Mapping[str, str],
    cwd: Path = ROOT,
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run a setup command, failing with its output when it does not exit cleanly."""
    printable = shlex.join([str(part) for part in command])
    print(f"$ {printable}", flush=True)
    try:
        result = subprocess.run(
            list(command),
            cwd=cwd,
            env=dict(env),
            text=True,
            capture_output=capture,
            check=False,
        )
    except OSError as exc:
        raise DevnetError(f"cannot run {printable}: {exc}") from exc
    if result.returncode != 0:
        detail = ""
        if capture:
            detail = (result.stderr or result.stdout or "").strip()
        suffix = f"\n{detail}" if detail else ""
        raise DevnetError(f"`{printable}` exited with {result.returncode}{suffix}")
    return result


class Runner:
    """Start, monitor and stop the child processes of one devnet run."""

    def __init__(self, log_dir: Path, env: Mapping[str, str], *, quiet: bool) -> None:
        self.log_dir = log_dir
        self.env = dict(env)
        self.quiet = quiet
        self.services: list[Service] = []
        self._stopped = False

    def start(
        self,
        name: str,
        command: Sequence[str],
        *,
        cwd: Path = ROOT,
        env: Mapping[str, str] | None = None,
    ) -> Service:
        """Start one service and begin copying its combined output to a log."""
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            log_path = self.log_dir / f"{name}.log"
            handle = log_path.open("w", encoding="utf-8")
        except OSError as exc:
            raise DevnetError(f"cannot start {name}: {exc}") from exc
        try:
            process = subprocess.Popen(
                list(command),
                cwd=cwd,
                env=dict(self.env if env is None else env),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                start_new_session=True,
            )
        except OSError as exc:
            handle.close()
            raise DevnetError(f"cannot start {name}: {exc}") from exc
        service = Service(
            name=name,
            command=tuple(str(part) for part in command),
            process=process,
            log_path=log_path,
            log_handle=handle,
            thread=threading.Thread(target=self._pump, args=(name, process, handle), daemon=True),
        )
        service.thread.start()
        self.services.append(service)
        return service

    def _pump(self, name: str, process: subprocess.Popen[str], handle: IO[str]) -> None:
        stream = process.stdout
        assert stream is not None
        for line in stream:
            handle.write(line)
            handle.flush()
            if not self.quiet:
                print(f"[{name}] {line}", end="", flush=True)
        handle.flush()

    def log_tail(self, service: Service) -> str:
        """The last few lines of a service log, for an actionable startup failure."""
        try:
            lines = service.log_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return ""
        return "\n".join(lines[-LOG_LINES:])

    def ensure_alive(self, service: Service) -> None:
        """Raise with the log tail when a child exited before we expected it to."""
        code = service.process.poll()
        if code is not None:
            tail = self.log_tail(service)
            suffix = f"\n{tail}" if tail else ""
            raise DevnetError(f"{service.name} exited with {code}; log: {service.log_path}{suffix}")

    def ensure_all_alive(self) -> None:
        for service in self.services:
            self.ensure_alive(service)

    def stop(self) -> None:
        """Terminate every child, then force any that did not exit in five seconds."""
        if self._stopped:
            return
        self._stopped = True
        for service in reversed(self.services):
            if service.process.poll() is None:
                _signal_service(service.process, signal.SIGTERM)
        for service in reversed(self.services):
            try:
                service.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _signal_service(service.process, signal.SIGKILL)
                with contextlib.suppress(subprocess.TimeoutExpired):
                    service.process.wait(timeout=2)
            service.thread.join(timeout=2)
            service.log_handle.close()


def _signal_service(process: subprocess.Popen[str], signum: int) -> None:
    """Signal a service's whole process group when the platform supports it."""
    try:
        if os.name == "posix":
            os.killpg(process.pid, signum)
        elif signum == signal.SIGTERM:
            process.terminate()
        else:
            process.kill()
    except ProcessLookupError:
        pass


def _assert_port_free(host: str, port: int, label: str) -> None:
    """Fail before setup when a stale service already owns a devnet port."""
    target = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.25)
            if sock.connect_ex((target, port)) == 0:
                raise DevnetError(
                    f"{label} port {port} is already in use; stop that service or choose "
                    f"another --{label.replace(' ', '-')}-port"
                )
    except OSError as exc:
        raise DevnetError(f"cannot check {label} port {port}: {exc}") from exc


def _check_ports(args: argparse.Namespace) -> None:
    """Check every port this run intends to bind."""
    _assert_port_free(args.host, args.node_port, "node")
    _assert_port_free(args.host, args.relay_port, "relay")
    _assert_port_free(args.host, args.witness_port, "witness")
    if args.web_ui == "dev":
        _assert_port_free(args.host, args.web_port, "web")


def _setup_stack(args: argparse.Namespace, stack: Path, env: Mapping[str, str]) -> None:
    """Write keys, bootstrap the chain and derive the relay config."""
    command = [
        sys.executable,
        str(ROOT / "deploy" / "setup_devnet_stack.py"),
        "--out",
        str(stack),
    ]
    if args.fresh:
        command.append("--fresh")
    _run_checked(command, env=env)


def _wallet_document(keyfile: Path, env: Mapping[str, str]) -> dict[str, Any]:
    """Read a keyfile's public header through the CLI and return its JSON object."""
    result = _run_checked(
        _sccp_command("--json", "wallet", "address", "--key", str(keyfile)),
        env=env,
        capture=True,
    )
    try:
        document = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise DevnetError(f"{keyfile} returned invalid wallet JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise DevnetError(f"{keyfile} returned a non-object wallet document")
    address = document.get("address")
    public_key = document.get("public_key")
    network = document.get("network")
    if not all(isinstance(value, str) and value for value in (address, public_key, network)):
        raise DevnetError(f"{keyfile} is missing its address, public key or network")
    if network != "devnet":
        raise DevnetError(f"{keyfile} belongs to {network!r}, not devnet")
    return document


def _ensure_key(
    name: str,
    path: Path,
    *,
    env: Mapping[str, str],
    passphrase_file: Path,
) -> KeyInfo:
    """Create a devnet keyfile once, then reuse it on later runs."""
    if path.is_file():
        print(f"  reusing {name}: {path}", flush=True)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        _run_checked(
            _sccp_command(
                "--json",
                "wallet",
                "new",
                "--out",
                str(path),
                passphrase_file=passphrase_file,
            ),
            env=env,
            capture=True,
        )
        print(f"  created {name}: {path}", flush=True)
    document = _wallet_document(path, env)
    return KeyInfo(
        name=name,
        path=path,
        address=str(document["address"]),
        public_key=str(document["public_key"]),
    )


def _load_miners(stack: Path) -> tuple[MinerInfo, ...]:
    """Read the two bootstrap miners the devnet stack wrote."""
    summary_path = stack / "stack.json"
    try:
        document = json.loads(summary_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise DevnetError(f"cannot read {summary_path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise DevnetError(f"{summary_path} is not valid JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise DevnetError(f"{summary_path} must contain a JSON object")
    raw_miners = document.get("miners")
    if not isinstance(raw_miners, list) or len(raw_miners) != MINER_COUNT:
        raise DevnetError(f"{summary_path} must list exactly {MINER_COUNT} miners")
    miners: list[MinerInfo] = []
    for raw in raw_miners:
        if not isinstance(raw, dict):
            raise DevnetError(f"each miner in {summary_path} must be an object")
        values = {field: raw.get(field) for field in ("name", "address", "keyfile", "nonce_file")}
        if not all(isinstance(value, str) and value for value in values.values()):
            raise DevnetError(f"each miner in {summary_path} needs a name, address and key files")
        miner = MinerInfo(
            name=str(values["name"]),
            address=str(values["address"]),
            keyfile=Path(str(values["keyfile"])),
            nonce_file=Path(str(values["nonce_file"])),
        )
        for label, path in (("keyfile", miner.keyfile), ("nonce file", miner.nonce_file)):
            if not path.is_file():
                raise DevnetError(f"{miner.name}'s {label} does not exist: {path}")
        miners.append(miner)
    return tuple(miners)


def _run_npm(arguments: Sequence[str], *, env: Mapping[str, str]) -> None:
    """Run an npm command in src/web with a useful error when npm is missing."""
    npm = shutil.which("npm")
    if npm is None:
        raise DevnetError("npm is required to build or run the web UI")
    _run_checked([npm, *arguments], env=env, cwd=WEB_DIR)


def _web_dist(args: argparse.Namespace) -> Path:
    """The built UI directory, honoring an explicit override."""
    if args.web_dist is not None:
        return args.web_dist.expanduser().resolve()
    return WEB_DIR / "dist"


def _prepare_web_ui(args: argparse.Namespace, env: dict[str, str]) -> bool:
    """Make sure the chosen UI can start; return whether the node must serve it."""
    if args.web_ui == "none":
        return False
    if args.web_ui == "dev":
        if shutil.which("npm") is None:
            raise DevnetError("npm is required to run the Vite dev server")
        if not (WEB_DIR / "node_modules").is_dir():
            if args.no_build_web:
                raise DevnetError("--web-ui dev needs src/web/node_modules; run npm ci first")
            _run_npm(["ci"], env=env)
        return False

    dist = _web_dist(args)
    index = dist / "index.html"
    if not index.is_file():
        if args.web_dist is not None:
            raise DevnetError(f"--web-dist {dist} does not contain index.html")
        if args.no_build_web:
            raise DevnetError(f"--no-build-web was given but {index} does not exist")
        if not (WEB_DIR / "node_modules").is_dir():
            _run_npm(["ci"], env=env)
        _run_npm(["run", "build"], env=env)
    if not index.is_file():
        raise DevnetError(f"the web build did not produce {index}")
    env["SCCP_WEB_DIST"] = str(dist)
    print(f"  built UI: {dist}", flush=True)
    return True


def _node_command(
    args: argparse.Namespace,
    *,
    data_dir: Path,
    miner_address: str,
    serve_web: bool,
) -> list[str]:
    command = _sccp_command(
        "node",
        "run",
        "--host",
        args.host,
        "--port",
        str(args.node_port),
        "--mine",
        "--miner-address",
        miner_address,
        "--no-mine-bootstrap",
        "--mine-poll-seconds",
        "0.5",
        data_dir=data_dir,
    )
    if serve_web:
        command.append("--web")
    return command


def _relay_command(
    args: argparse.Namespace,
    *,
    data_dir: Path,
    config: Path,
    node_url: str,
    witness_key: Path,
    passphrase_file: Path,
    origins: Sequence[str],
) -> list[str]:
    command = _sccp_command(
        "matchmaker",
        "run",
        "--config",
        str(config),
        "--host",
        args.host,
        "--port",
        str(args.relay_port),
        "--node",
        node_url,
        "--witness-key",
        str(witness_key),
        "--allow-unwitnessed-rounds",
        "--sweep-seconds",
        "0.5",
        "--node-poll-seconds",
        "1",
        data_dir=data_dir,
        passphrase_file=passphrase_file,
    )
    for origin in origins:
        command.extend(["--origin", origin])
    return command


def _miner_command(
    args: argparse.Namespace,
    *,
    data_dir: Path,
    miner: MinerInfo,
    relay_url: str,
    node_url: str,
    relay_witness_key_file: Path,
    passphrase_file: Path,
) -> list[str]:
    command = _sccp_command(
        "miner",
        "run",
        "--key",
        str(miner.keyfile),
        "--nonce-secret-file",
        str(miner.nonce_file),
        "--relay",
        relay_url,
        "--node",
        node_url,
        "--engine-profile",
        "mock",
        "--poll-seconds",
        "0.5",
        "--relay-witness-key-file",
        str(relay_witness_key_file),
        data_dir=data_dir,
        passphrase_file=passphrase_file,
    )
    if args.resign_after_plies:
        command.extend(["--resign-after-plies", str(args.resign_after_plies)])
    return command


def _witness_command(
    args: argparse.Namespace,
    *,
    data_dir: Path,
    clock_key: Path,
    relay_url: str,
    relay_witness_key_file: Path,
    passphrase_file: Path,
) -> list[str]:
    return _sccp_command(
        "witness",
        "run",
        "--key",
        str(clock_key),
        "--relay",
        relay_url,
        "--host",
        args.host,
        "--port",
        str(args.witness_port),
        "--relay-witness-key-file",
        str(relay_witness_key_file),
        "--poll-seconds",
        "1",
        data_dir=data_dir,
        passphrase_file=passphrase_file,
    )


def _http_json(url: str, *, timeout: float = 1.0) -> dict[str, Any] | None:
    """GET a JSON object, returning ``None`` until the service answers."""
    try:
        with urlopen(url, timeout=timeout) as response:
            if response.status != 200:
                return None
            document = json.load(response)
    except (OSError, ValueError, URLError):
        return None
    return document if isinstance(document, dict) else None


def _http_ok(url: str, *, timeout: float = 1.0) -> bool:
    """Whether a URL answers with a non-server-error status."""
    try:
        with urlopen(url, timeout=timeout) as response:
            return response.status < 500
    except (OSError, URLError):
        return False


def _wait_for_json(
    runner: Runner,
    service: Service,
    url: str,
    *,
    expected_network: str | None,
    timeout: float,
) -> dict[str, Any]:
    """Wait for a service's ``/v1/status`` and fail immediately if it exits."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        runner.ensure_alive(service)
        document = _http_json(url)
        if document is not None:
            network = document.get("network")
            if expected_network is None or network == expected_network:
                return document
        time.sleep(0.25)
    runner.ensure_alive(service)
    raise DevnetError(
        f"{service.name} did not answer {url} within {timeout:g}s; log: {service.log_path}"
    )


def _wait_for_page(runner: Runner, service: Service, url: str, *, timeout: float) -> None:
    """Wait for the Vite dev server to serve its index page."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        runner.ensure_alive(service)
        if _http_ok(url):
            return
        time.sleep(0.25)
    runner.ensure_alive(service)
    raise DevnetError(
        f"{service.name} did not answer {url} within {timeout:g}s; log: {service.log_path}"
    )


def _step(number: int, total: int, message: str) -> None:
    print(f"[{number}/{total}] {message}", flush=True)


def _raise_keyboard_interrupt(_signum: int, _frame: FrameType | None) -> None:
    raise KeyboardInterrupt


def main(argv: Sequence[str] | None = None) -> int:
    """Parse the arguments and run the devnet until interrupted."""
    args = _parser().parse_args(argv)
    signal.signal(signal.SIGINT, _raise_keyboard_interrupt)
    signal.signal(signal.SIGTERM, _raise_keyboard_interrupt)
    if hasattr(signal, "pthread_sigmask"):
        # A background shell can leave SIGINT blocked; Ctrl+C must still stop us.
        signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGINT, signal.SIGTERM})
    runner: Runner | None = None
    try:
        _validate_arguments(args)
        stack = args.stack.expanduser().resolve()
        passphrase = _stack_passphrase(args, stack)
        env = _environment(args, passphrase)
        _check_ports(args)

        total = 8
        _step(1, total, "Preparing the web UI")
        serve_web = _prepare_web_ui(args, env)
        _step(2, total, "Bootstrapping the devnet chain and miner keys")
        _setup_stack(args, stack, env)

        passphrase_file = stack / PASSPHRASE_FILE
        if not passphrase_file.is_file():
            raise DevnetError(f"the devnet stack did not write {passphrase_file}")
        _step(3, total, "Creating or reusing the devnet wallet")
        wallet = _ensure_key(
            "devnet wallet",
            stack / "keys" / "devnet-wallet.key",
            env=env,
            passphrase_file=passphrase_file,
        )
        relay_key = _ensure_key(
            "relay witness key",
            stack / "keys" / "relay-witness.key",
            env=env,
            passphrase_file=passphrase_file,
        )
        clock_key = _ensure_key(
            "clock witness key",
            stack / "keys" / "clock-witness.key",
            env=env,
            passphrase_file=passphrase_file,
        )
        relay_public_key = stack / "keys" / "relay-witness.pub"
        relay_public_key.write_text(relay_key.public_key + "\n", encoding="utf-8")
        miners = _load_miners(stack)

        data_dir = stack / "chain"
        relay_config = stack / "relay.json"
        host = _local_host(args.host)
        node_url = f"http://{host}:{args.node_port}"
        relay_url = f"http://{host}:{args.relay_port}"
        witness_url = f"http://{host}:{args.witness_port}"
        web_url = node_url if args.web_ui == "built" else f"http://{host}:{args.web_port}"
        origins = [node_url]
        if args.web_ui == "dev":
            origins.append(web_url)

        runner = Runner(stack / "logs", env, quiet=args.quiet)
        _step(4, total, f"Starting the devnet node on {node_url}")
        node = runner.start(
            "node",
            _node_command(
                args,
                data_dir=data_dir,
                miner_address=miners[0].address,
                serve_web=serve_web,
            ),
        )
        node_status = _wait_for_json(
            runner,
            node,
            f"{node_url}/v1/status",
            expected_network="devnet",
            timeout=args.ready_timeout,
        )
        print(f"  chain height {node_status['height']}", flush=True)

        if args.web_ui == "dev":
            _step(5, total, f"Starting the Vite dev server on {web_url}")
            web = runner.start(
                "web-ui",
                ["npm", "run", "dev", "--", "--host", args.host, "--port", str(args.web_port)],
                cwd=WEB_DIR,
                env={**env, "SCCP_NODE": node_url},
            )
            _wait_for_page(runner, web, web_url, timeout=args.ready_timeout)

        _step(6, total, f"Starting the devnet relay on {relay_url}")
        relay = runner.start(
            "relay",
            _relay_command(
                args,
                data_dir=data_dir,
                config=relay_config,
                node_url=node_url,
                witness_key=relay_key.path,
                passphrase_file=passphrase_file,
                origins=origins,
            ),
        )
        relay_status = _wait_for_json(
            runner,
            relay,
            f"{relay_url}/v1/status",
            expected_network="devnet",
            timeout=args.ready_timeout,
        )
        print(f"  relay epoch {relay_status['epoch']}, round {relay_status['round']}", flush=True)

        _step(7, total, f"Starting the clock witness on {witness_url}")
        witness = runner.start(
            "witness",
            _witness_command(
                args,
                data_dir=data_dir,
                clock_key=clock_key.path,
                relay_url=relay_url,
                relay_witness_key_file=relay_public_key,
                passphrase_file=passphrase_file,
            ),
        )
        witness_status = _wait_for_json(
            runner,
            witness,
            f"{witness_url}/v1/status",
            expected_network="devnet",
            timeout=args.ready_timeout,
        )
        print(f"  witness {witness_status['address']}", flush=True)

        _step(8, total, f"Starting {MINER_COUNT} devnet miners")
        miner_services = [
            runner.start(
                f"miner-{index + 1}",
                _miner_command(
                    args,
                    data_dir=data_dir,
                    miner=miner,
                    relay_url=relay_url,
                    node_url=node_url,
                    relay_witness_key_file=relay_public_key,
                    passphrase_file=passphrase_file,
                ),
            )
            for index, miner in enumerate(miners)
        ]
        time.sleep(1.0)
        for service in miner_services:
            runner.ensure_alive(service)

        print(flush=True)
        print("SCCP devnet is up", flush=True)
        print(f"  web UI       {web_url if args.web_ui != 'none' else 'disabled'}", flush=True)
        print(f"  node API     {node_url}/v1/status", flush=True)
        print(f"  relay API    {relay_url}/v1/status", flush=True)
        print(f"  witness API  {witness_url}/v1/status", flush=True)
        print(f"  wallet       {wallet.address}", flush=True)
        print(f"  wallet file  {wallet.path}", flush=True)
        for miner in miners:
            print(f"  miner        {miner.name} {miner.address}", flush=True)
        print(f"  logs         {stack / 'logs'}", flush=True)
        print("Press Ctrl+C to stop every process.", flush=True)

        while True:
            runner.ensure_all_alive()
            time.sleep(2.0)
    except KeyboardInterrupt:
        print("\nstopping the devnet", flush=True)
        return 0
    except DevnetError as exc:
        print(f"error: {exc}", file=sys.stderr, flush=True)
        return 1
    finally:
        if runner is not None:
            runner.stop()


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
