"""End-to-end tests for the CLI (in-process, no subprocess)."""

from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any, ClassVar

import chess
import pytest
from typer.testing import CliRunner

from sccp.chess.pgn import validate_pgn
from sccp.cli import PASSPHRASE_ENV, app
from sccp.config import DEVNET, UNITS_PER_COIN
from sccp.crypto.address import address_version, encode_address
from sccp.crypto.pq import generate_hybrid_keypair, hybrid_keypair_from_seed
from sccp.crypto.wallet import read_header
from sccp.errors import RelayError
from sccp.protocol.transcript import (
    build_win_proof,
    create_result,
    expected_from_certificate,
    render_transcript_pgn,
    sign_result,
)
from sccp.transport import client_for
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
    assert set(payload) == {"address", "network", "public_key", "scheme", "keyfile"}
    assert payload["scheme"] == "ed25519-mldsa65"
    assert payload["network"] == "testnet"
    assert payload["address"].startswith("sccpt1")
    # 1-byte tag + 32-byte Ed25519 key + 1952-byte ML-DSA-65 key.
    assert len(bytes.fromhex(payload["public_key"])) == 1985
    assert address_version(payload["address"]) == 1

    header = read_header(tmp_path / "w.key")
    assert header.version == 2
    assert header.scheme == "ed25519-mldsa65"
    assert header.address == payload["address"]

    addressed = runner.invoke(
        app, ["--json", "wallet", "address", "--key", str(tmp_path / "w.key")]
    )
    assert addressed.exit_code == 0, addressed.output
    assert json.loads(addressed.stdout)["scheme"] == "ed25519-mldsa65"


def test_the_scheme_flag_is_gone(tmp_path: Path, env: dict[str, str]) -> None:
    """There is one scheme, so ``--scheme`` must not exist at all."""
    result = runner.invoke(
        app,
        ["wallet", "new", "--out", str(tmp_path / "w.key"), "--scheme", "ed25519", *FAST],
        env=env,
    )
    assert result.exit_code == 2
    assert "No such option" in result.stderr


def test_miner_commands_do_not_refuse_a_hybrid_key() -> None:
    """Every keyfile is hybrid, so the old classical-only guard is gone."""
    import sccp.cli as cli

    assert not hasattr(cli, "_classical_keypair")


def test_export_and_import_hybrid_mnemonic(tmp_path: Path, env: dict[str, str]) -> None:
    created = _new(tmp_path, env)
    assert created.exit_code == 0, created.output
    source = str(tmp_path / "wallet.key")

    exported = runner.invoke(app, ["wallet", "export-mnemonic", "--key", source], env=env)
    assert exported.exit_code == 0, exported.output
    phrase = exported.stdout.strip()
    assert len(phrase.split()) == 24

    exported_json = runner.invoke(
        app, ["--json", "wallet", "export-mnemonic", "--key", source], env=env
    )
    assert exported_json.exit_code == 0, exported_json.output
    document = json.loads(exported_json.stdout)
    assert document["scheme"] == "ed25519-mldsa65"
    assert document["address"] == created.stdout.strip()
    assert document["mnemonic"] == phrase

    restored = tmp_path / "restored.key"
    imported = runner.invoke(
        app,
        ["wallet", "import-mnemonic", "--out", str(restored), *FAST],
        input=phrase + "\n",
        env=env,
    )
    assert imported.exit_code == 0, imported.output
    assert imported.stdout.strip().splitlines()[-1] == created.stdout.strip()
    assert read_header(restored).scheme == "ed25519-mldsa65"


def test_import_mnemonic_reads_the_phrase_from_a_file(tmp_path: Path, env: dict[str, str]) -> None:
    created = _new(tmp_path, env)
    assert created.exit_code == 0, created.output
    exported = runner.invoke(
        app, ["wallet", "export-mnemonic", "--key", str(tmp_path / "wallet.key")], env=env
    )
    assert exported.exit_code == 0, exported.output

    backup = tmp_path / "wallet.mnemonic"
    backup.write_text(exported.stdout, encoding="utf-8")
    backup.chmod(0o600)
    restored = tmp_path / "restored.key"
    # No stdin at all: the file is the whole input, which is what makes this
    # usable from a script and from a shell without a terminal.
    imported = runner.invoke(
        app,
        [
            "wallet",
            "import-mnemonic",
            "--out",
            str(restored),
            "--mnemonic-file",
            str(backup),
            *FAST,
        ],
        env=env,
    )
    assert imported.exit_code == 0, imported.output
    assert imported.stdout.strip().splitlines()[-1] == created.stdout.strip()

    backup.chmod(0o644)
    exposed = runner.invoke(
        app,
        [
            "wallet",
            "import-mnemonic",
            "--out",
            str(tmp_path / "other.key"),
            "--mnemonic-file",
            str(backup),
            *FAST,
        ],
        env=env,
    )
    assert exposed.exit_code == 1
    assert "chmod 600" in exposed.output


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


def test_devnet_up_prints_a_summary() -> None:
    result = runner.invoke(app, ["devnet", "up", "--miners", "2", "--blocks", "4"])
    assert result.exit_code == 0, result.output
    assert "height 4" in result.stdout
    assert "proofs mined" in result.stdout


def test_devnet_up_json_payload() -> None:
    result = runner.invoke(app, ["--json", "devnet", "up", "--miners", "2", "--blocks", "4"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["height"] == 4
    assert payload["network"] == "devnet"
    assert payload["proofs"]


def test_devnet_up_only_runs_devnet() -> None:
    result = runner.invoke(app, ["--network", "testnet", "devnet", "up"])
    assert result.exit_code == 2
    assert "only runs the devnet network" in result.stderr


def test_devnet_help_lists_up() -> None:
    result = runner.invoke(app, ["devnet", "--help"])
    assert result.exit_code == 0
    assert "up" in result.stdout


# --- Node, miner, chain and faucet commands ---------------------------------


@pytest.fixture()
def data_dir(tmp_path: Path) -> Path:
    """A ``--data-dir`` holding a genesis-only devnet chain."""
    from sccp.chain.storage import Storage, persist_chain
    from sccp.node.service import NodeService

    path = tmp_path / "data"
    path.mkdir()
    service = NodeService.open(DEVNET)
    storage = Storage(path / "chain.sqlite")
    try:
        persist_chain(service.chain, storage)
    finally:
        storage.close()
        service.close()
    return path


@pytest.fixture()
def faucet_key(tmp_path: Path, passphrase: str) -> Path:
    """A keyfile for the devnet faucet, which the genesis funds."""
    from sccp.chain.genesis import faucet_keypair
    from sccp.crypto.wallet import KdfParams, create_wallet

    path = tmp_path / "faucet.key"
    create_wallet(
        path,
        "devnet",
        passphrase,
        seed=faucet_keypair("devnet").seed,
        kdf=KdfParams(n=1024),
        created_at=1_700_000_000,
    )
    return path


class _RecordingNode:
    """Stand-in for ``NodeClient`` that records what the CLI would submit.

    ``accounts`` models ``GET /v1/accounts/{address}``. An address that is not
    registered raises, which is what a node that is not running does: the command
    then falls back to ``--data-dir``.
    """

    submitted: ClassVar[list[Any]] = []
    accounts: ClassVar[dict[str, dict[str, object]]] = {}

    def __init__(self, url: str = "", **_: Any) -> None:
        self.url = url

    def account(self, address: str) -> dict[str, object]:
        try:
            return _RecordingNode.accounts[address]
        except KeyError:
            raise RelayError(f"cannot reach {self.url}: no account for {address}") from None

    def submit_transaction(self, tx: Any) -> dict[str, object]:
        _RecordingNode.submitted.append(tx)
        return {"network": tx.network, "txid": txid_of(tx), "accepted": True}

    def close(self) -> None:
        return None


def txid_of(tx: Any) -> str:
    from sccp.chain.tx import txid

    return txid(tx)


@pytest.fixture()
def recorded_node(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Patch the node client the CLI submits through and return its receipts."""
    import sccp.cli as cli

    _RecordingNode.submitted = []
    _RecordingNode.accounts = {}
    monkeypatch.setattr(cli, "NodeClient", _RecordingNode)
    return _RecordingNode.submitted


@pytest.fixture()
def node_accounts(recorded_node: list[Any]) -> dict[str, dict[str, object]]:
    """The accounts the stand-in node answers ``/v1/accounts`` with."""
    assert recorded_node == []
    return _RecordingNode.accounts


def account_document(address: str, *, spendable: int, nonce: int) -> dict[str, object]:
    """The document ``GET /v1/accounts/{address}`` returns for a funded account."""
    return {
        "network": DEVNET.network_id,
        "address": address,
        "balance": spendable,
        "immature": 0,
        "bonded": 0,
        "total": spendable,
        "nonce": nonce,
        "registered_until_epoch": None,
        "endpoint": None,
        "engine": None,
        "public_key": None,
        "banned_until": 0,
    }


MINER_ADDRESS = encode_address(hybrid_keypair_from_seed(bytes([9]) * 32).public_key_bytes, DEVNET)
NONCE_SECRET = "11" * 32


def test_m4_help_lists_the_new_commands() -> None:
    for group, command in (
        ("node", "run"),
        ("miner", "run"),
        ("miner", "register"),
        ("miner", "availability"),
        ("miner", "reveal"),
        ("chain", "info"),
        ("chain", "blocks"),
        ("chain", "account"),
        ("chain", "verify"),
        ("wallet", "balance"),
        ("wallet", "send"),
        ("wallet", "history"),
        ("faucet", "send"),
    ):
        result = runner.invoke(app, [group, "--help"])
        assert result.exit_code == 0, result.output
        assert command in result.stdout, (group, command)


def test_chain_commands_read_the_local_database(
    data_dir: Path, faucet_key: Path, env: dict[str, str]
) -> None:
    info = runner.invoke(
        app, ["--json", "--data-dir", str(data_dir), "chain", "info", "--network", "devnet"]
    )
    assert info.exit_code == 0, info.output
    payload = json.loads(info.stdout)
    assert payload["network"] == "devnet"
    assert payload["height"] == 0
    assert payload["difficulty"]["wins_required"] == DEVNET.wins_required

    blocks = runner.invoke(
        app, ["--json", "--data-dir", str(data_dir), "chain", "blocks", "--network", "devnet"]
    )
    assert blocks.exit_code == 0, blocks.output
    assert json.loads(blocks.stdout)[0]["height"] == 0

    address = runner.invoke(app, ["wallet", "address", "--key", str(faucet_key)])
    account = runner.invoke(
        app,
        [
            "--json",
            "--data-dir",
            str(data_dir),
            "chain",
            "account",
            address.stdout.strip(),
            "--network",
            "devnet",
        ],
    )
    assert account.exit_code == 0, account.output
    assert json.loads(account.stdout)["total"] > 0

    verify = runner.invoke(
        app, ["--data-dir", str(data_dir), "chain", "verify", "--network", "devnet"]
    )
    assert verify.exit_code == 0, verify.output
    assert "ok: 1 blocks replay to height 0" in verify.stdout


def test_chain_commands_need_a_database(tmp_path: Path) -> None:
    result = runner.invoke(app, ["--data-dir", str(tmp_path / "empty"), "chain", "info"])
    assert result.exit_code == 1
    output = " ".join(result.output.split())
    assert "no chain database" in output
    assert f"run `sccp --data-dir {tmp_path / 'empty'} node run --network testnet` first" in output


def test_node_run_serves_the_app(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import uvicorn
    from fastapi import FastAPI

    served: dict[str, Any] = {}

    def fake_run(application: Any, *, host: str, port: int) -> None:
        served.update(app=application, host=host, port=port)

    monkeypatch.setattr(uvicorn, "run", fake_run)
    result = runner.invoke(
        app,
        ["--data-dir", str(data_dir), "node", "run", "--network", "devnet", "--port", "18645"],
    )
    assert result.exit_code == 0, result.output
    assert "height 0" in result.stdout
    assert served["host"] == "127.0.0.1" and served["port"] == 18645
    assert isinstance(served["app"], FastAPI)


def test_node_run_validates_the_mining_options(data_dir: Path) -> None:
    missing = runner.invoke(
        app, ["--data-dir", str(data_dir), "node", "run", "--network", "devnet", "--mine"]
    )
    assert missing.exit_code == 2
    assert "--mine needs --miner-address" in missing.output

    invalid = runner.invoke(
        app,
        [
            "--data-dir",
            str(data_dir),
            "node",
            "run",
            "--network",
            "devnet",
            "--mine",
            "--miner-address",
            "sccpt1nope",
        ],
    )
    assert invalid.exit_code == 2
    assert "not a valid devnet address" in invalid.output

    bad_poll = runner.invoke(
        app,
        [
            "--data-dir",
            str(data_dir),
            "node",
            "run",
            "--network",
            "devnet",
            "--mine",
            "--miner-address",
            _devnet_address(),
            "--mine-poll-seconds",
            "0",
        ],
    )
    assert bad_poll.exit_code == 2
    assert "--mine-poll-seconds must be positive" in bad_poll.output

    bad_interval = runner.invoke(
        app,
        [
            "--data-dir",
            str(data_dir),
            "node",
            "run",
            "--network",
            "devnet",
            "--mine",
            "--miner-address",
            _devnet_address(),
            "--mine-interval-seconds",
            "-1",
        ],
    )
    assert bad_interval.exit_code == 2
    assert "--mine-interval-seconds must not be negative" in bad_interval.output


def test_node_run_starts_the_settlement_miner(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import uvicorn

    served: dict[str, Any] = {}
    monkeypatch.setattr(
        uvicorn, "run", lambda application, *, host, port: served.update(app=application)
    )
    result = runner.invoke(
        app,
        [
            "--data-dir",
            str(data_dir),
            "node",
            "run",
            "--network",
            "devnet",
            "--mine",
            "--miner-address",
            _devnet_address(),
            "--no-mine-bootstrap",
        ],
    )
    assert result.exit_code == 0, result.output
    assert f"mining proof blocks for {_devnet_address()}" in result.stdout
    assert served["app"] is not None


def test_node_run_can_hide_the_faucet(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import uvicorn

    served: dict[str, Any] = {}
    monkeypatch.setattr(
        uvicorn, "run", lambda application, *, host, port: served.update(app=application)
    )
    result = runner.invoke(
        app,
        ["--data-dir", str(data_dir), "node", "run", "--network", "devnet", "--no-faucet"],
    )
    assert result.exit_code == 0, result.output
    with client_for(served["app"]) as client:
        response = client.get("/v1/faucet")
    assert response.status_code == 404
    assert response.json()["detail"] == "the faucet is disabled"


def _devnet_address() -> str:
    return encode_address(hybrid_keypair_from_seed(bytes([9]) * 32).public_key_bytes, DEVNET)


def test_node_run_warns_about_a_peer_that_is_down(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import uvicorn

    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: None)
    result = runner.invoke(
        app,
        [
            "--data-dir",
            str(data_dir),
            "node",
            "run",
            "--network",
            "devnet",
            "--peer",
            "http://127.0.0.1:1",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "did not answer" in result.output


def test_miner_run_reports_its_ticks(
    tmp_path: Path, env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from sccp.miner.engine_miner import EngineMiner, MinerTick

    assert _new(tmp_path, env, "--network", "devnet").exit_code == 0
    keyfile = tmp_path / "wallet.key"
    ticks = iter(
        [
            MinerTick(observed=1, joined=("m1",), moves=("m1:e4",)),
            MinerTick(observed=1, results=("m1:result",), proofs=("p1",)),
        ]
    )
    monkeypatch.setattr(EngineMiner, "run_once", lambda self: next(ticks))
    result = runner.invoke(
        app,
        [
            "--json",
            "miner",
            "run",
            "--network",
            "devnet",
            "--key",
            str(keyfile),
            "--engine-profile",
            "mock",
            "--relay",
            "http://127.0.0.1:8646",
            "--iterations",
            "2",
            "--poll-seconds",
            "0.01",
        ],
        env=env,
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["network"] == "devnet"
    assert payload["polls"] == 2
    assert payload["joined"] == ["m1"]
    assert payload["proofs"] == ["p1"]
    assert payload["resignations"] == []


def test_matchmaker_run_refuses_bad_sweep_settings(tmp_path: Path) -> None:
    config = tmp_path / "relay.json"
    config.write_text("{}", encoding="utf-8")
    result = runner.invoke(
        app, ["matchmaker", "run", "--config", str(config), "--sweep-seconds", "0"]
    )
    assert result.exit_code == 2
    assert "--sweep-seconds must be positive" in result.output


def test_miner_run_passes_the_relay_witness_keys_through(
    tmp_path: Path, env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from sccp.miner.engine_miner import EngineMiner, MinerTick

    assert _new(tmp_path, env, "--network", "devnet").exit_code == 0
    witness = generate_hybrid_keypair()
    seen: list[dict[str, str]] = []

    def capture(self: EngineMiner) -> MinerTick:
        seen.append(self.config.witness_key_map)
        return MinerTick()

    monkeypatch.setattr(EngineMiner, "run_once", capture)
    result = runner.invoke(
        app,
        [
            "--json",
            "miner",
            "run",
            "--network",
            "devnet",
            "--key",
            str(tmp_path / "wallet.key"),
            "--engine-profile",
            "mock",
            "--relay-witness-key",
            witness.public_key_hex,
            "--iterations",
            "1",
            "--poll-seconds",
            "0.01",
        ],
        env=env,
    )
    assert result.exit_code == 0, result.output
    address = encode_address(witness.public_key_bytes, DEVNET)
    assert seen == [{address: witness.public_key_hex}]


def test_miner_run_refuses_a_zero_ply_resignation_limit(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "miner",
            "run",
            "--key",
            str(tmp_path / "wallet.key"),
            "--resign-after-plies",
            "0",
        ],
    )
    assert result.exit_code == 2
    assert "--resign-after-plies must be at least 1 ply" in result.output


def test_miner_run_passes_the_resignation_policy_through(
    tmp_path: Path, env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from sccp.miner.engine_miner import EngineMiner, MinerTick

    assert _new(tmp_path, env, "--network", "devnet").exit_code == 0
    seen: list[int | None] = []

    def capture(self: EngineMiner) -> MinerTick:
        seen.append(self.config.resign_after_plies)
        return MinerTick(resignations=("m1",))

    monkeypatch.setattr(EngineMiner, "run_once", capture)
    result = runner.invoke(
        app,
        [
            "--json",
            "miner",
            "run",
            "--network",
            "devnet",
            "--key",
            str(tmp_path / "wallet.key"),
            "--engine-profile",
            "mock",
            "--resign-after-plies",
            "60",
            "--iterations",
            "1",
            "--poll-seconds",
            "0.01",
        ],
        env=env,
    )
    assert result.exit_code == 0, result.output
    assert seen == [60]
    assert json.loads(result.stdout)["resignations"] == ["m1"]


def test_miner_run_refuses_a_bad_relay_witness_key(tmp_path: Path, env: dict[str, str]) -> None:
    assert _new(tmp_path, env, "--network", "devnet").exit_code == 0
    result = runner.invoke(
        app,
        [
            "miner",
            "run",
            "--network",
            "devnet",
            "--key",
            str(tmp_path / "wallet.key"),
            "--engine-profile",
            "mock",
            "--relay-witness-key",
            "not-hex",
        ],
        env=env,
    )
    assert result.exit_code == 2
    assert "not valid hex" in result.output


def test_miner_run_needs_a_binary_for_a_real_engine(tmp_path: Path, env: dict[str, str]) -> None:
    assert _new(tmp_path, env, "--network", "devnet").exit_code == 0
    result = runner.invoke(
        app,
        ["miner", "run", "--network", "devnet", "--key", str(tmp_path / "wallet.key")],
        env=env,
    )
    assert result.exit_code == 1
    assert "needs an explicit binary" in result.output


def test_miner_run_reads_the_availability_secret_file(
    tmp_path: Path, env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from sccp.miner.engine_miner import EngineMiner, MinerTick

    assert _new(tmp_path, env, "--network", "devnet").exit_code == 0
    secret_file = tmp_path / "miner-1.nonce"
    secret_file.write_text("11" * 32 + "\n", encoding="utf-8")
    seen: list[str | None] = []

    def capture(self: EngineMiner) -> MinerTick:
        seen.append(self.config.nonce_secret)
        return MinerTick()

    monkeypatch.setattr(EngineMiner, "run_once", capture)
    result = runner.invoke(
        app,
        [
            "--json",
            "miner",
            "run",
            "--network",
            "devnet",
            "--key",
            str(tmp_path / "wallet.key"),
            "--engine-profile",
            "mock",
            "--nonce-secret-file",
            str(secret_file),
            "--iterations",
            "1",
            "--poll-seconds",
            "0.01",
        ],
        env=env,
    )
    assert result.exit_code == 0, result.output
    assert seen == ["11" * 32]


def test_miner_run_refuses_a_bad_availability_secret(tmp_path: Path, env: dict[str, str]) -> None:
    assert _new(tmp_path, env, "--network", "devnet").exit_code == 0
    base = [
        "miner",
        "run",
        "--network",
        "devnet",
        "--key",
        str(tmp_path / "wallet.key"),
        "--engine-profile",
        "mock",
    ]
    not_hex = runner.invoke(app, [*base, "--nonce-secret", "not-hex"], env=env)
    assert not_hex.exit_code == 2
    assert "not valid hex" in not_hex.output

    short = runner.invoke(app, [*base, "--nonce-secret", "11" * 31], env=env)
    assert short.exit_code == 2
    assert "must be 32 bytes" in short.output

    secret_file = tmp_path / "miner-1.nonce"
    secret_file.write_text("11" * 32 + "\n", encoding="utf-8")
    both = runner.invoke(
        app,
        [*base, "--nonce-secret", "11" * 32, "--nonce-secret-file", str(secret_file)],
        env=env,
    )
    assert both.exit_code == 2
    assert "not both" in both.output


def test_miner_availability_reports_the_reveal_deadline(
    data_dir: Path,
    faucet_key: Path,
    env: dict[str, str],
    recorded_node: list[Any],
) -> None:
    """The commit names the tip by which the reveal has to be built.

    On a genesis-only chain the next block is in epoch 1, so availability targets
    ``epoch 1 + availability_lead`` = 3. A reveal for epoch 3 is only valid in a
    block of epoch 2, which is block 2 here: build it on tip 1 at the latest.
    """
    base = [
        "--data-dir",
        str(data_dir),
        "miner",
        "availability",
        "--network",
        "devnet",
        "--key",
        str(faucet_key),
        "--max-games",
        "3",
        "--node",
        "http://127.0.0.1:8645",
    ]
    human = runner.invoke(app, base, env=env)
    assert human.exit_code == 0, human.output
    assert "committed availability for epoch 3 (3 games)" in human.stdout
    assert "reveal by tip 1 (block 2 is the last of epoch 2)" in human.stdout
    assert len(recorded_node) == 1

    machine = runner.invoke(app, ["--json", *base], env=env)
    assert machine.exit_code == 0, machine.output
    payload = json.loads(machine.stdout)
    assert payload["epoch"] == 3
    assert payload["reveal_by_tip"] == 1
    assert payload["max_games"] == 3
    assert len(payload["nonce_secret"]) == 64


def test_miner_run_warns_that_the_secret_needs_a_node(
    tmp_path: Path, env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from sccp.miner.engine_miner import EngineMiner, MinerTick

    assert _new(tmp_path, env, "--network", "devnet").exit_code == 0
    monkeypatch.setattr(EngineMiner, "run_once", lambda self: MinerTick())
    result = runner.invoke(
        app,
        [
            "miner",
            "run",
            "--network",
            "devnet",
            "--key",
            str(tmp_path / "wallet.key"),
            "--engine-profile",
            "mock",
            "--nonce-secret",
            "11" * 32,
            "--iterations",
            "1",
            "--poll-seconds",
            "0.01",
        ],
        env=env,
    )
    assert result.exit_code == 0, result.output
    assert "needs --node" in result.output


def test_miner_run_refuses_bad_poll_settings(tmp_path: Path, env: dict[str, str]) -> None:

    assert _new(tmp_path, env, "--network", "devnet").exit_code == 0
    result = runner.invoke(
        app,
        [
            "miner",
            "run",
            "--network",
            "devnet",
            "--key",
            str(tmp_path / "wallet.key"),
            "--engine-profile",
            "mock",
            "--poll-seconds",
            "0",
        ],
        env=env,
    )
    assert result.exit_code == 2
    assert "must be positive" in result.output


def test_wallet_balance_and_empty_history(
    data_dir: Path, faucet_key: Path, env: dict[str, str]
) -> None:
    balance = runner.invoke(
        app,
        [
            "--data-dir",
            str(data_dir),
            "wallet",
            "balance",
            "--network",
            "devnet",
            "--key",
            str(faucet_key),
        ],
        env=env,
    )
    assert balance.exit_code == 0, balance.output
    assert "1000000.00000000" in balance.stdout

    history = runner.invoke(
        app,
        [
            "--data-dir",
            str(data_dir),
            "wallet",
            "history",
            "--network",
            "devnet",
            "--key",
            str(faucet_key),
        ],
        env=env,
    )
    assert history.exit_code == 0, history.output
    assert "no transactions" in history.stdout


def test_wallet_send_submits_a_signed_transfer(
    data_dir: Path, faucet_key: Path, env: dict[str, str], recorded_node: list[Any]
) -> None:
    result = runner.invoke(
        app,
        [
            "--data-dir",
            str(data_dir),
            "wallet",
            "send",
            "--network",
            "devnet",
            "--key",
            str(faucet_key),
            "--to",
            MINER_ADDRESS,
            "--amount",
            "10",
        ],
        env=env,
    )
    assert result.exit_code == 0, result.output
    assert "sent 10.00000000" in result.stdout
    assert len(recorded_node) == 1
    tx = recorded_node[0]
    assert tx.type == "transfer"
    assert tx.payload["amount"] == 1_000_000_000


def test_wallet_send_works_with_only_a_node_and_no_chain_database(
    tmp_path: Path,
    env: dict[str, str],
    recorded_node: list[Any],
    node_accounts: dict[str, dict[str, object]],
) -> None:
    """A node that answers supplies the balance and nonce: no sync, no --data-dir."""
    data_dir = tmp_path / "no-chain"
    data_dir.mkdir()
    key = tmp_path / "alice.key"
    created = runner.invoke(
        app, ["wallet", "new", "--out", str(key), "--network", "devnet", *FAST], env=env
    )
    assert created.exit_code == 0, created.output
    address = created.stdout.strip()
    node_accounts[address] = account_document(address, spendable=50 * UNITS_PER_COIN, nonce=7)

    result = runner.invoke(
        app,
        [
            "--data-dir",
            str(data_dir),
            "wallet",
            "send",
            "--network",
            "devnet",
            "--key",
            str(key),
            "--to",
            MINER_ADDRESS,
            "--amount",
            "10",
            "--node",
            "http://testnet.example:8645",
        ],
        env=env,
    )
    assert result.exit_code == 0, result.output
    assert "sent 10.00000000" in result.stdout
    assert not (data_dir / "chain.sqlite").exists()
    assert len(recorded_node) == 1
    transfer = recorded_node[0]
    assert transfer.nonce == 7
    assert transfer.payload["amount"] == 10 * UNITS_PER_COIN


def test_wallet_send_takes_the_nonce_from_the_node_not_the_local_chain(
    data_dir: Path,
    faucet_key: Path,
    env: dict[str, str],
    recorded_node: list[Any],
    node_accounts: dict[str, dict[str, object]],
) -> None:
    """The node checks the nonce, so its answer wins over a chain that lags it."""
    from sccp.chain.genesis import faucet_keypair

    address = encode_address(faucet_keypair("devnet").public_key_bytes, DEVNET)
    node_accounts[address] = account_document(address, spendable=5 * UNITS_PER_COIN, nonce=9)

    result = runner.invoke(
        app,
        [
            "--data-dir",
            str(data_dir),
            "wallet",
            "send",
            "--network",
            "devnet",
            "--key",
            str(faucet_key),
            "--to",
            MINER_ADDRESS,
            "--amount",
            "4",
            "--node",
            "http://testnet.example:8645",
        ],
        env=env,
    )
    assert result.exit_code == 0, result.output
    assert recorded_node[0].nonce == 9


def test_wallet_send_refusal_names_the_node_it_read_the_balance_from(
    data_dir: Path,
    faucet_key: Path,
    env: dict[str, str],
    recorded_node: list[Any],
    node_accounts: dict[str, dict[str, object]],
) -> None:
    """A refusal built on a node's numbers quotes the node, not --data-dir."""
    from sccp.chain.genesis import faucet_keypair

    address = encode_address(faucet_keypair("devnet").public_key_bytes, DEVNET)
    node_accounts[address] = account_document(address, spendable=5 * UNITS_PER_COIN, nonce=0)

    result = runner.invoke(
        app,
        [
            "--data-dir",
            str(data_dir),
            "wallet",
            "send",
            "--network",
            "devnet",
            "--key",
            str(faucet_key),
            "--to",
            MINER_ADDRESS,
            "--amount",
            "12",
            "--node",
            "http://testnet.example:8645",
        ],
        env=env,
    )
    assert result.exit_code == 1
    assert "has 5.00000000 spendable" in result.stderr
    assert "this balance came from the node at http://testnet.example:8645" in result.stderr
    assert recorded_node == []


def test_wallet_send_reports_a_node_it_cannot_read(
    data_dir: Path, faucet_key: Path, env: dict[str, str], recorded_node: list[Any]
) -> None:
    """An explicit --node that does not answer stops the command; it does not sign."""
    result = runner.invoke(
        app,
        [
            "--data-dir",
            str(data_dir),
            "wallet",
            "send",
            "--network",
            "devnet",
            "--key",
            str(faucet_key),
            "--to",
            MINER_ADDRESS,
            "--amount",
            "10",
            "--node",
            "http://testnet.example:8645",
        ],
        env=env,
    )
    assert result.exit_code == 1
    assert "cannot read" in result.stderr
    assert "http://testnet.example:8645" in result.stderr
    assert recorded_node == []


def test_wallet_balance_reads_the_node_without_a_chain_database(
    tmp_path: Path,
    env: dict[str, str],
    recorded_node: list[Any],
    node_accounts: dict[str, dict[str, object]],
) -> None:
    """`wallet balance --node` works off a node alone, and says where it read."""
    data_dir = tmp_path / "no-chain"
    data_dir.mkdir()
    key = tmp_path / "bob.key"
    created = runner.invoke(
        app, ["wallet", "new", "--out", str(key), "--network", "devnet", *FAST], env=env
    )
    assert created.exit_code == 0, created.output
    address = created.stdout.strip()
    node_accounts[address] = account_document(address, spendable=50 * UNITS_PER_COIN, nonce=2)

    result = runner.invoke(
        app,
        [
            "--json",
            "--data-dir",
            str(data_dir),
            "wallet",
            "balance",
            "--network",
            "devnet",
            "--key",
            str(key),
            "--node",
            "http://testnet.example:8645",
        ],
        env=env,
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["spendable"] == 50 * UNITS_PER_COIN
    assert payload["source"] == "http://testnet.example:8645"
    assert not (data_dir / "chain.sqlite").exists()
    assert recorded_node == []


def test_wallet_balance_falls_back_when_the_default_node_is_another_network(
    data_dir: Path,
    faucet_key: Path,
    env: dict[str, str],
    recorded_node: list[Any],
    node_accounts: dict[str, dict[str, object]],
) -> None:
    """A default node on another network must not hide the chain --data-dir holds."""
    from sccp.chain.genesis import faucet_keypair

    address = encode_address(faucet_keypair("devnet").public_key_bytes, DEVNET)
    node_accounts[address] = {
        **account_document(address, spendable=0, nonce=0),
        "network": "mainnet",
    }

    result = runner.invoke(
        app,
        [
            "--json",
            "--data-dir",
            str(data_dir),
            "wallet",
            "balance",
            "--network",
            "devnet",
            "--key",
            str(faucet_key),
        ],
        env=env,
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["spendable"] == 1_000_000 * UNITS_PER_COIN
    assert payload["source"] == str(data_dir / "chain.sqlite")

    # Named explicitly, the same node is a mistake worth stopping for.
    rejected = runner.invoke(
        app,
        [
            "--data-dir",
            str(data_dir),
            "wallet",
            "balance",
            "--network",
            "devnet",
            "--key",
            str(faucet_key),
            "--node",
            "http://mainnet.example:8645",
        ],
        env=env,
    )
    assert rejected.exit_code == 1
    assert "serves mainnet, not devnet" in rejected.stderr
    assert recorded_node == []


def test_wallet_send_transfers_between_two_new_wallets(
    tmp_path: Path,
    data_dir: Path,
    env: dict[str, str],
    recorded_node: list[Any],
) -> None:
    """Create two wallets, fund one, and send coins to the other through the CLI."""
    from sccp.chain.genesis import faucet_keypair
    from sccp.chain.mempool import Mempool
    from sccp.chain.storage import Storage
    from sccp.config import coins_to_units
    from sccp.miner.block_builder import BlockBuilder
    from sccp.node.service import NodeService
    from sccp.wallet.service import WalletService

    alice_key = tmp_path / "alice.key"
    bob_key = tmp_path / "bob.key"
    wallets = (("alice", alice_key), ("bob", bob_key))
    addresses: dict[str, str] = {}
    for name, path in wallets:
        created = runner.invoke(
            app,
            ["wallet", "new", "--out", str(path), "--network", "devnet", *FAST],
            env=env,
        )
        assert created.exit_code == 0, created.output
        addresses[name] = created.stdout.strip()

    # New wallets start empty, so mine a faucet transfer that funds Alice.
    faucet = faucet_keypair("devnet")
    miner = hybrid_keypair_from_seed(bytes([9]) * 32)
    service = NodeService.open(DEVNET, storage=Storage(data_dir / "chain.sqlite"))
    try:
        pool = Mempool(params=DEVNET, tip_state=service.chain.state, book=service.chain.book)
        pool.add_transaction(
            WalletService(DEVNET, pool.projection).build_transfer(
                faucet, addresses["alice"], coins_to_units(100)
            )
        )
        funding = BlockBuilder(
            params=DEVNET, chain=service.chain, mempool=pool, keypair=miner
        ).build(timestamp=1_700_000_001)
        assert funding is not None
        assert service.ingest_block(funding).accepted
    finally:
        service.close()

    sent = runner.invoke(
        app,
        [
            "--data-dir",
            str(data_dir),
            "wallet",
            "send",
            "--network",
            "devnet",
            "--key",
            str(alice_key),
            "--to",
            addresses["bob"],
            "--amount",
            "10",
        ],
        env=env,
    )
    assert sent.exit_code == 0, sent.output
    assert f"sent 10.00000000 to {addresses['bob']}" in sent.stdout
    assert len(recorded_node) == 1
    transfer = recorded_node[0]
    assert transfer.sender == addresses["alice"]
    assert transfer.payload["to"] == addresses["bob"]
    assert transfer.payload["amount"] == 10 * UNITS_PER_COIN

    # The recording node does not mine, so put the submitted transfer in a block
    # before reading the balances back through the CLI.
    service = NodeService.open(DEVNET, storage=Storage(data_dir / "chain.sqlite"))
    try:
        pool = Mempool(params=DEVNET, tip_state=service.chain.state, book=service.chain.book)
        pool.add_transaction(transfer)
        block = BlockBuilder(params=DEVNET, chain=service.chain, mempool=pool, keypair=miner).build(
            timestamp=1_700_000_002
        )
        assert block is not None
        assert service.ingest_block(block).accepted
    finally:
        service.close()

    balances: dict[str, Any] = {}
    for name, path in wallets:
        result = runner.invoke(
            app,
            [
                "--json",
                "--data-dir",
                str(data_dir),
                "wallet",
                "balance",
                "--network",
                "devnet",
                "--key",
                str(path),
            ],
            env=env,
        )
        assert result.exit_code == 0, result.output
        balances[name] = json.loads(result.stdout)

    assert balances["alice"]["spendable"] == (
        100 * UNITS_PER_COIN - 10 * UNITS_PER_COIN - DEVNET.min_fee
    )
    assert balances["bob"]["spendable"] == 10 * UNITS_PER_COIN


def test_wallet_send_refuses_an_empty_chain_in_coins(
    tmp_path: Path, data_dir: Path, env: dict[str, str], recorded_node: list[Any]
) -> None:
    """A genesis-only chain reports zero spendable; say why, in coins."""
    key = tmp_path / "empty.key"
    created = runner.invoke(
        app, ["wallet", "new", "--out", str(key), "--network", "devnet", *FAST], env=env
    )
    assert created.exit_code == 0, created.output

    result = runner.invoke(
        app,
        [
            "--data-dir",
            str(data_dir),
            "wallet",
            "send",
            "--network",
            "devnet",
            "--key",
            str(key),
            "--to",
            MINER_ADDRESS,
            "--amount",
            "12",
        ],
        env=env,
    )
    assert result.exit_code == 1
    # 12 coins plus the minimum fee, printed as coins rather than base units.
    assert "has 0.00000000 spendable" in result.stderr
    assert "needs 12.00001000 (12.00000000 plus a 0.00001000 fee)" in result.stderr
    assert "holds only the genesis block" in result.stderr
    assert recorded_node == []


def test_wallet_balance_points_out_a_chain_with_no_blocks(
    tmp_path: Path, data_dir: Path, env: dict[str, str], recorded_node: list[Any]
) -> None:
    key = tmp_path / "empty.key"
    created = runner.invoke(
        app, ["wallet", "new", "--out", str(key), "--network", "devnet", *FAST], env=env
    )
    assert created.exit_code == 0, created.output

    result = runner.invoke(
        app,
        [
            "--data-dir",
            str(data_dir),
            "wallet",
            "balance",
            "--network",
            "devnet",
            "--key",
            str(key),
        ],
        env=env,
    )
    assert result.exit_code == 0, result.output
    assert "spendable 0.00000000" in result.stdout
    assert "holds only the genesis block" in result.stderr


def test_wallet_balance_is_quiet_on_a_funded_genesis_chain(
    data_dir: Path, faucet_key: Path, env: dict[str, str], recorded_node: list[Any]
) -> None:
    """The note explains an empty balance only; genesis funds are not a puzzle."""
    result = runner.invoke(
        app,
        [
            "--data-dir",
            str(data_dir),
            "wallet",
            "balance",
            "--network",
            "devnet",
            "--key",
            str(faucet_key),
        ],
        env=env,
    )
    assert result.exit_code == 0, result.output
    assert "spendable 1000000.00000000" in result.stdout
    assert result.stderr == ""


def test_miner_register_availability_and_reveal(
    data_dir: Path,
    faucet_key: Path,
    env: dict[str, str],
    recorded_node: list[Any],
) -> None:
    common = ["--data-dir", str(data_dir)]

    register = runner.invoke(
        app,
        [
            *common,
            "miner",
            "register",
            "--network",
            "devnet",
            "--key",
            str(faucet_key),
            "--endpoint",
            "tcp://alice.example:8646",
            "--bond",
            "10",
        ],
        env=env,
    )
    assert register.exit_code == 0, register.output
    assert "registered" in register.stdout

    availability = runner.invoke(
        app,
        [
            *common,
            "miner",
            "availability",
            "--network",
            "devnet",
            "--key",
            str(faucet_key),
            "--max-games",
            "3",
            "--nonce-secret",
            NONCE_SECRET,
        ],
        env=env,
    )
    assert availability.exit_code == 0, availability.output
    assert NONCE_SECRET in availability.stdout

    reveal = runner.invoke(
        app,
        [
            *common,
            "miner",
            "reveal",
            "--network",
            "devnet",
            "--key",
            str(faucet_key),
            "--nonce-secret",
            NONCE_SECRET,
        ],
        env=env,
    )
    assert reveal.exit_code == 0, reveal.output
    assert "revealed for epoch" in reveal.stdout

    assert [tx.type for tx in recorded_node] == ["register", "availability", "reveal"]
    assert recorded_node[0].payload["bond"] == 1_000_000_000


def test_faucet_send_is_limited_to_test_networks(
    data_dir: Path, env: dict[str, str], recorded_node: list[Any]
) -> None:
    result = runner.invoke(
        app,
        [
            "--data-dir",
            str(data_dir),
            "faucet",
            "send",
            "--network",
            "devnet",
            "--to",
            MINER_ADDRESS,
            "--amount",
            "1000",
        ],
        env=env,
    )
    assert result.exit_code == 0, result.output
    assert "faucet sent 1000.00000000" in result.stdout
    assert recorded_node[0].payload["amount"] == 100_000_000_000

    rejected = runner.invoke(
        app,
        ["--data-dir", str(data_dir), "faucet", "send", "--network", "mainnet", "--to", "x"],
        env=env,
    )
    assert rejected.exit_code == 2
    assert "has no faucet" in rejected.output


def test_faucet_send_reads_the_faucet_account_from_the_node(
    tmp_path: Path,
    env: dict[str, str],
    node_accounts: dict[str, dict[str, object]],
    recorded_node: list[Any],
) -> None:
    from sccp.chain.genesis import faucet_keypair

    sender = encode_address(faucet_keypair(DEVNET.network_id).public_key_bytes, DEVNET)
    node_accounts[sender] = account_document(sender, spendable=500_000_000_000, nonce=5)
    result = runner.invoke(
        app,
        [
            "--data-dir",
            str(tmp_path / "no-chain-here"),
            "faucet",
            "send",
            "--network",
            "devnet",
            "--to",
            MINER_ADDRESS,
            "--amount",
            "10",
            "--node",
            "http://node.test:8645",
        ],
        env=env,
    )
    assert result.exit_code == 0, result.output
    assert "faucet sent 10.00000000" in result.stdout
    assert recorded_node[0].nonce == 5, "the nonce must be the one the node reported"
    assert recorded_node[0].sender == sender


def test_faucet_send_cites_the_node_that_reported_no_coins(
    data_dir: Path,
    env: dict[str, str],
    node_accounts: dict[str, dict[str, object]],
    recorded_node: list[Any],
) -> None:
    from sccp.chain.genesis import faucet_keypair

    sender = encode_address(faucet_keypair(DEVNET.network_id).public_key_bytes, DEVNET)
    node_accounts[sender] = account_document(sender, spendable=0, nonce=0)
    result = runner.invoke(
        app,
        [
            "--data-dir",
            str(data_dir),
            "faucet",
            "send",
            "--network",
            "devnet",
            "--to",
            MINER_ADDRESS,
            "--amount",
            "10",
            "--node",
            "http://node.test:8645",
        ],
        env=env,
    )
    assert result.exit_code == 1
    assert "0.00000000 spendable" in result.output
    assert "this balance came from the node at http://node.test:8645" in result.output
    assert "genesis block" not in result.output, "a node answered, so --data-dir is not at fault"
    assert recorded_node == []


def test_devnet_up_accepts_only_mock_engines() -> None:
    ok = runner.invoke(app, ["devnet", "up", "--miners", "2", "--blocks", "2", "--engines", "mock"])
    assert ok.exit_code == 0, ok.output
    rejected = runner.invoke(app, ["devnet", "up", "--engines", "stockfish-19"])
    assert rejected.exit_code == 2
    assert "only supports --engines mock" in rejected.output
