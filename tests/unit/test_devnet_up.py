"""``scripts/devnet_up.py``: the commands and preflight checks of the orchestrator.

The script is deliberately composed of small builders: one per child process plus
the port/passphrase checks that run before anything starts. These tests pin the
flags those builders hand to the CLI, so a renamed CLI option fails here instead
of halfway through a live devnet start.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from argparse import Namespace
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "devnet_up.py"
"""The script under test."""


@pytest.fixture(scope="module")
def devnet_up() -> ModuleType:
    """Import ``scripts/devnet_up.py`` by path, without installing it."""
    spec = importlib.util.spec_from_file_location("sccp_devnet_up", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _args(**overrides: object) -> Namespace:
    """A complete options namespace with the script's defaults."""
    values: dict[str, object] = {
        "stack": Path(".devnet-stack"),
        "fresh": False,
        "host": "127.0.0.1",
        "node_port": 8645,
        "relay_port": 8646,
        "witness_port": 8647,
        "web_port": 5173,
        "web_ui": "built",
        "web_dist": None,
        "no_build_web": False,
        "ready_timeout": 30.0,
        "resign_after_plies": 60,
        "passphrase": None,
        "quiet": False,
    }
    values.update(overrides)
    return Namespace(**values)


def test_defaults_match_the_documented_devnet_ports(devnet_up: ModuleType) -> None:
    args = devnet_up._parser().parse_args([])
    assert (args.node_port, args.relay_port, args.witness_port, args.web_port) == (
        8645,
        8646,
        8647,
        5173,
    )
    assert args.web_ui == "built"
    assert args.resign_after_plies == 60


def test_node_command_mines_and_serves_the_built_ui(devnet_up: ModuleType, tmp_path: Path) -> None:
    command = devnet_up._node_command(
        _args(),
        data_dir=tmp_path / "chain",
        miner_address="sccpt1miner",
        serve_web=True,
    )
    assert command[:4] == [sys.executable, "-m", "sccp.cli", "--network"]
    assert "--mine" in command
    assert command[command.index("--miner-address") + 1] == "sccpt1miner"
    assert "--no-mine-bootstrap" in command
    assert command[-1] == "--web"


def test_relay_command_follows_the_node_and_allows_devnet_rounds(
    devnet_up: ModuleType, tmp_path: Path
) -> None:
    command = devnet_up._relay_command(
        _args(),
        data_dir=tmp_path / "chain",
        config=tmp_path / "relay.json",
        node_url="http://127.0.0.1:8645",
        witness_key=tmp_path / "relay.key",
        passphrase_file=tmp_path / "passphrase",
        origins=("http://127.0.0.1:8645", "http://127.0.0.1:5173"),
    )
    assert command[3:5] == ["--network", "devnet"]
    assert command[5:8] == ["--data-dir", str(tmp_path / "chain"), "--passphrase-file"]
    assert "--allow-unwitnessed-rounds" in command
    assert command[command.index("--node") + 1] == "http://127.0.0.1:8645"
    assert command[command.index("--witness-key") + 1] == str(tmp_path / "relay.key")
    origins = [command[index + 1] for index, part in enumerate(command) if part == "--origin"]
    assert origins == ["http://127.0.0.1:8645", "http://127.0.0.1:5173"]


def test_miner_command_uses_the_stack_secret_and_mock_engine(
    devnet_up: ModuleType, tmp_path: Path
) -> None:
    miner = devnet_up.MinerInfo(
        name="miner-1",
        address="sccpt1miner",
        keyfile=tmp_path / "miner-1.key",
        nonce_file=tmp_path / "miner-1.nonce",
    )
    command = devnet_up._miner_command(
        _args(),
        data_dir=tmp_path / "chain",
        miner=miner,
        relay_url="http://127.0.0.1:8646",
        node_url="http://127.0.0.1:8645",
        relay_witness_key_file=tmp_path / "relay.pub",
        passphrase_file=tmp_path / "passphrase",
    )
    assert command[command.index("--nonce-secret-file") + 1] == str(miner.nonce_file)
    assert command[command.index("--engine-profile") + 1] == "mock"
    assert command[command.index("--relay-witness-key-file") + 1] == str(tmp_path / "relay.pub")
    assert command[command.index("--resign-after-plies") + 1] == "60"


def test_miner_command_can_disable_resignation(devnet_up: ModuleType, tmp_path: Path) -> None:
    miner = devnet_up.MinerInfo(
        name="miner-1",
        address="sccpt1miner",
        keyfile=tmp_path / "miner-1.key",
        nonce_file=tmp_path / "miner-1.nonce",
    )
    command = devnet_up._miner_command(
        _args(resign_after_plies=0),
        data_dir=tmp_path / "chain",
        miner=miner,
        relay_url="http://127.0.0.1:8646",
        node_url="http://127.0.0.1:8645",
        relay_witness_key_file=tmp_path / "relay.pub",
        passphrase_file=tmp_path / "passphrase",
    )
    assert "--resign-after-plies" not in command


def test_witness_command_binds_its_own_port_and_trusts_the_relay(
    devnet_up: ModuleType, tmp_path: Path
) -> None:
    command = devnet_up._witness_command(
        _args(),
        data_dir=tmp_path / "chain",
        clock_key=tmp_path / "clock.key",
        relay_url="http://127.0.0.1:8646",
        relay_witness_key_file=tmp_path / "relay.pub",
        passphrase_file=tmp_path / "passphrase",
    )
    assert command[command.index("--relay") + 1] == "http://127.0.0.1:8646"
    assert command[command.index("--port") + 1] == "8647"
    assert command[command.index("--relay-witness-key-file") + 1] == str(tmp_path / "relay.pub")


@pytest.mark.parametrize(
    "overrides",
    [
        {"node_port": 0},
        {"relay_port": 70000},
        {"ready_timeout": 0},
        {"resign_after_plies": -1},
        {"node_port": 8646},
        {"web_ui": "dev", "web_dist": Path("dist")},
    ],
)
def test_validation_rejects_impossible_runs(
    devnet_up: ModuleType, overrides: dict[str, object]
) -> None:
    with pytest.raises(devnet_up.DevnetError):
        devnet_up._validate_arguments(_args(**overrides))


def test_passphrase_prefers_the_existing_stack_file(
    devnet_up: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SCCP_DEVNET_STACK_PASSPHRASE", raising=False)
    (tmp_path / "passphrase").write_text("existing-stack-passphrase\n", encoding="utf-8")
    assert devnet_up._stack_passphrase(_args(), tmp_path) == "existing-stack-passphrase"


def test_environment_carries_the_source_path_and_passphrase(
    devnet_up: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PYTHONPATH", "/existing")
    env = devnet_up._environment(_args(), "a-devnet-passphrase")
    assert env["PYTHONPATH"].startswith(str(devnet_up.ROOT / "src"))
    assert env["PYTHONPATH"].endswith("/existing")
    assert env["SCCP_DEVNET_STACK_PASSPHRASE"] == "a-devnet-passphrase"
    assert env["PYTHONUNBUFFERED"] == "1"


def test_custom_web_dist_must_already_hold_an_index(devnet_up: ModuleType, tmp_path: Path) -> None:
    with pytest.raises(devnet_up.DevnetError, match=r"does not contain index\.html"):
        devnet_up._prepare_web_ui(_args(web_dist=tmp_path / "missing"), {})


def test_load_miners_reads_the_two_bootstrap_identities(
    devnet_up: ModuleType, tmp_path: Path
) -> None:
    miners = []
    for index in (1, 2):
        keyfile = tmp_path / f"miner-{index}.key"
        nonce = tmp_path / f"miner-{index}.nonce"
        keyfile.write_text("{}", encoding="utf-8")
        nonce.write_text("00" * 32, encoding="utf-8")
        miners.append(
            {
                "name": f"miner-{index}",
                "address": f"sccpt1miner{index}",
                "keyfile": str(keyfile),
                "nonce_file": str(nonce),
            }
        )
    (tmp_path / "stack.json").write_text(
        json.dumps({"network": "devnet", "miners": miners}), encoding="utf-8"
    )
    loaded = devnet_up._load_miners(tmp_path)
    assert [miner.name for miner in loaded] == ["miner-1", "miner-2"]


def test_load_miners_refuses_a_stack_without_exactly_two(
    devnet_up: ModuleType, tmp_path: Path
) -> None:
    (tmp_path / "stack.json").write_text(
        json.dumps({"network": "devnet", "miners": [{}]}), encoding="utf-8"
    )
    with pytest.raises(devnet_up.DevnetError, match="exactly 2"):
        devnet_up._load_miners(tmp_path)
