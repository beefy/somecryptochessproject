"""End-to-end tests for the CLI (in-process, no subprocess)."""

from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from sccp.cli import PASSPHRASE_ENV, app
from sccp.version import version_string

runner = CliRunner()
FAST = ["--kdf-n", "1024", "--created-at", "1700000000"]


@pytest.fixture()
def env(passphrase: str) -> dict[str, str]:
    return {PASSPHRASE_ENV: passphrase}


def _new(tmp_path: Path, env: dict[str, str], *extra: str) -> Any:
    path = tmp_path / "wallet.key"
    return runner.invoke(app, ["wallet", "new", "--out", str(path), *FAST, *extra], env=env)


def test_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == version_string()


def test_help_lists_wallet_commands() -> None:
    result = runner.invoke(app, ["wallet", "--help"])
    assert result.exit_code == 0
    for command in ("new", "address", "export-mnemonic", "import-mnemonic"):
        assert command in result.stdout


def test_new_prints_address_and_writes_keyfile(
    tmp_path: Path, env: dict[str, str], vectors: dict[str, Any]
) -> None:
    result = _new(tmp_path, env)
    assert result.exit_code == 0, result.output
    address = result.stdout.strip()
    assert address.startswith("sccpt1")

    keyfile = tmp_path / "wallet.key"
    assert stat.S_IMODE(keyfile.stat().st_mode) == 0o600
    document = json.loads(keyfile.read_text(encoding="utf-8"))
    assert document["address"] == address
    assert document["network"] == "testnet"
    assert document["created_at"] == 1700000000

    again = runner.invoke(app, ["wallet", "address", "--key", str(keyfile)])
    assert again.exit_code == 0
    assert again.stdout.strip() == address


def test_new_json_output(tmp_path: Path, env: dict[str, str]) -> None:
    result = runner.invoke(
        app, ["--json", "wallet", "new", "--out", str(tmp_path / "w.key"), *FAST], env=env
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert set(payload) == {"address", "network", "public_key", "keyfile"}
    assert payload["network"] == "testnet"
    assert payload["address"].startswith("sccpt1")


def test_network_override_and_mismatch(tmp_path: Path, env: dict[str, str]) -> None:
    mainnet = _new(tmp_path, env, "--network", "mainnet")
    assert mainnet.exit_code == 0, mainnet.output
    assert mainnet.stdout.strip().startswith("sccp1")
    keyfile = str(tmp_path / "wallet.key")

    ok = runner.invoke(app, ["wallet", "address", "--key", keyfile, "--network", "mainnet"])
    assert ok.exit_code == 0
    assert ok.stdout.strip() == mainnet.stdout.strip()

    clash = runner.invoke(app, ["--network", "testnet", "wallet", "address", "--key", keyfile])
    assert clash.exit_code == 1
    assert "belongs to network 'mainnet'" in clash.stderr


def test_existing_keyfile_requires_force(tmp_path: Path, env: dict[str, str]) -> None:
    assert _new(tmp_path, env).exit_code == 0
    clash = _new(tmp_path, env)
    assert clash.exit_code == 1
    assert "already exists" in clash.stderr
    forced = _new(tmp_path, env, "--force")
    assert forced.exit_code == 0, forced.output


def test_unknown_network_exits_two(tmp_path: Path, env: dict[str, str]) -> None:
    result = runner.invoke(
        app,
        ["--network", "regtest", "wallet", "new", "--out", str(tmp_path / "w.key"), *FAST],
        env=env,
    )
    assert result.exit_code == 2
    assert "regtest" in result.stderr


def test_export_and_import_mnemonic(
    tmp_path: Path, env: dict[str, str], vectors: dict[str, Any]
) -> None:
    assert _new(tmp_path, env).exit_code == 0
    source = str(tmp_path / "wallet.key")

    exported = runner.invoke(app, ["wallet", "export-mnemonic", "--key", source], env=env)
    assert exported.exit_code == 0, exported.output
    phrase = exported.stdout.strip()
    assert len(phrase.split()) == 24

    restored = tmp_path / "restored.key"
    imported = runner.invoke(
        app,
        ["wallet", "import-mnemonic", "--out", str(restored), *FAST],
        input=phrase + "\n",
        env=env,
    )
    assert imported.exit_code == 0, imported.output
    original = runner.invoke(app, ["wallet", "address", "--key", source])
    assert imported.stdout.strip().splitlines()[-1] == original.stdout.strip()


def test_passphrase_file(tmp_path: Path, passphrase: str) -> None:
    secret = tmp_path / "pass.txt"
    secret.write_text(passphrase + "\n", encoding="utf-8")
    secret.chmod(0o600)
    keyfile = tmp_path / "wallet.key"
    global_args = ["--passphrase-file", str(secret)]

    created = runner.invoke(
        app,
        [*global_args, "wallet", "new", "--out", str(keyfile), *FAST],
        env={PASSPHRASE_ENV: ""},
    )
    assert created.exit_code == 0, created.output

    exported = runner.invoke(
        app,
        [*global_args, "wallet", "export-mnemonic", "--key", str(keyfile)],
        env={PASSPHRASE_ENV: ""},
    )
    assert exported.exit_code == 0, exported.output
    assert len(exported.stdout.split()) == 24


def test_wrong_passphrase_is_reported(tmp_path: Path, env: dict[str, str]) -> None:
    assert _new(tmp_path, env).exit_code == 0
    wrong = runner.invoke(
        app,
        ["wallet", "export-mnemonic", "--key", str(tmp_path / "wallet.key")],
        env={PASSPHRASE_ENV: "wrong"},
    )
    assert wrong.exit_code == 1
    assert "cannot decrypt" in wrong.stderr
