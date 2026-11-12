"""End-to-end tests for the CLI (in-process, no subprocess)."""

from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any

import chess
import pytest
from typer.testing import CliRunner

from sccp.chess.pgn import validate_pgn
from sccp.cli import PASSPHRASE_ENV, app
from sccp.config import DEVNET
from sccp.protocol.transcript import (
    build_win_proof,
    create_result,
    expected_from_certificate,
    render_transcript_pgn,
    sign_result,
)
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


def test_help_lists_verify_commands() -> None:
    result = runner.invoke(app, ["verify", "--help"])
    assert result.exit_code == 0
    assert "pgn" in result.stdout


def test_verify_pgn(tmp_path: Path, match_tags: dict[str, str]) -> None:
    pgn = tmp_path / "game.pgn"
    headers = "".join(f'[{tag} "{value}"]\n' for tag, value in match_tags.items())
    pgn.write_text(f"{headers}\n1. f3 e5 2. g4 Qh4# 0-1\n", encoding="utf-8")

    human = runner.invoke(app, ["verify", "pgn", str(pgn)])
    assert human.exit_code == 0, human.output
    assert "game hash:" in human.stdout
    assert "0-1" in human.stdout

    machine = runner.invoke(app, ["--json", "verify", "pgn", str(pgn), "--network", "testnet"])
    assert machine.exit_code == 0, machine.output
    payload = json.loads(machine.stdout)
    assert payload["result"] == "0-1"
    assert payload["winner"] == "black"
    assert payload["plies"] == 4
    assert len(payload["game_hash"]) == 64


def test_verify_pgn_rejects_a_bad_game(tmp_path: Path) -> None:
    pgn = tmp_path / "bad.pgn"
    pgn.write_text("1. e4 e5 2. Ke3 1-0\n", encoding="utf-8")
    result = runner.invoke(app, ["verify", "pgn", str(pgn)])
    assert result.exit_code == 1
    assert "error:" in result.stderr


def test_verify_pgn_require_win(tmp_path: Path, match_tags: dict[str, str]) -> None:
    pgn = tmp_path / "short.pgn"
    headers = "".join(f'[{tag} "{value}"]\n' for tag, value in match_tags.items())
    pgn.write_text(f"{headers}\n1. f3 e5 2. g4 Qh4# 0-1\n", encoding="utf-8")

    result = runner.invoke(app, ["verify", "pgn", str(pgn), "--require-win"])
    assert result.exit_code == 1
    assert "at least" in result.stderr


def _proof_files(signed_match, tmp_path: Path, proof_document=None) -> dict[str, Path]:
    proof = tmp_path / "proof.json"
    document = proof_document or signed_match.proof.model_dump(mode="json")
    proof.write_text(json.dumps(document), encoding="utf-8")

    context = tmp_path / "context.json"
    context.write_text(
        json.dumps(
            {
                "network": signed_match.params.network_id,
                "beacon": signed_match.beacon.model_dump(mode="json"),
                "roster": {
                    address: {
                        "public_key": signed_match.public_keys[address],
                        "max_games": 3,
                    }
                    for address in signed_match.roster
                },
                "availability": {
                    availability.miner: availability.commitment
                    for availability in signed_match.availability.values()
                },
            }
        ),
        encoding="utf-8",
    )

    book = tmp_path / "openings.json"
    book.write_text(
        json.dumps(
            {
                "format": 1,
                "books": {
                    signed_match.params.book_version: {
                        "lines": [
                            {
                                "name": "Test Italian",
                                "moves": ["e4", "e5", "Bc4", "Bc5"],
                            }
                        ]
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return {"proof": proof, "context": context, "book": book}


def _devnet_resignation_proof(signed_match) -> dict[str, Any]:
    """A 21-ply, decisive resignation proof valid under stock devnet params."""
    certificate = signed_match.certificate
    moves = (
        "e4",
        "e5",
        "Bc4",
        "Bc5",
        "Nf3",
        "Nc6",
        "d3",
        "d6",
        "Nc3",
        "Nf6",
        "Be3",
        "Be6",
        "Qd2",
        "Qd7",
        "O-O",
        "O-O-O",
        "a3",
        "a6",
        "b3",
        "h6",
        "h3",
    )
    board = chess.Board()
    for san in moves:
        board.push(board.parse_san(san))
    assert board.outcome(claim_draw=False) is None
    assert board.turn == chess.BLACK
    pgn = render_transcript_pgn(
        certificate,
        board,
        result="1-0",
        termination="resignation",
    )
    parsed = validate_pgn(
        pgn,
        params=DEVNET,
        expected=expected_from_certificate(certificate),
        require_win=True,
    )
    result = create_result(certificate, parsed)
    by_address = {account.address: account for account in signed_match.accounts.values()}
    result = sign_result(result, certificate, "white", by_address[certificate.white].keypair)
    result = sign_result(result, certificate, "black", by_address[certificate.black].keypair)
    proof = build_win_proof(
        certificate,
        pgn,
        result.result_signatures,
        params=DEVNET,
    )
    return proof.model_dump(mode="json")


def test_verify_proof_cli(signed_match, tmp_path: Path) -> None:
    document = _devnet_resignation_proof(signed_match)
    paths = _proof_files(signed_match, tmp_path, document)
    result = runner.invoke(
        app,
        [
            "--json",
            "verify",
            "proof",
            str(paths["proof"]),
            "--context",
            str(paths["context"]),
            "--book",
            str(paths["book"]),
            "--network",
            "devnet",
        ],
    )
    assert result.exit_code == 0, result.output + result.stderr
    payload = json.loads(result.stdout)
    assert payload["proof_id"] == document["proof_id"]
    assert payload["game_hash"] == document["game_hash"]
    assert payload["winner_seat"] == document["winner_seat"]


def test_verify_proof_cli_rejects_a_tampered_proof(signed_match, tmp_path: Path) -> None:
    document = _devnet_resignation_proof(signed_match)
    document["proof_id"] = "00" * 32
    paths = _proof_files(signed_match, tmp_path, document)

    result = runner.invoke(
        app,
        [
            "verify",
            "proof",
            str(paths["proof"]),
            "--context",
            str(paths["context"]),
            "--book",
            str(paths["book"]),
            "--network",
            "devnet",
        ],
    )
    assert result.exit_code == 1
    assert "error:" in result.stderr
    assert "proof id" in result.stderr


def test_verify_proof_cli_rejects_bad_json(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    result = runner.invoke(
        app,
        ["verify", "proof", str(bad), "--context", str(bad)],
    )
    assert result.exit_code == 1
    assert "not valid JSON" in result.stderr


def test_matchmaker_help_lists_run() -> None:
    result = runner.invoke(app, ["matchmaker", "--help"])
    assert result.exit_code == 0
    assert "run" in result.stdout
