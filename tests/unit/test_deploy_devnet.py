"""The deterministic devnet stack: keys, bootstrap chain and relay config."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from deploy import bootstrap_devnet_stack as bootstrap
from deploy.init_devnet_stack import write_stack

from sccp.chain.block import bootstrap_end_height
from sccp.chain.storage import Storage
from sccp.config import DEVNET
from sccp.miner.matchmaker import Matchmaker
from sccp.node.service import NodeService
from sccp.protocol.models import TxType


def test_the_stack_writes_two_devnet_keyfiles(tmp_path: Path) -> None:
    stack = tmp_path / "stack"
    summary = write_stack(stack, passphrase="test-devnet-passphrase")
    assert summary["network"] == "devnet"
    assert summary["availability_epoch"] == bootstrap.BOOTSTRAP_EPOCH
    entries = summary["miners"]
    assert isinstance(entries, list)
    assert [entry["name"] for entry in entries] == ["miner-1", "miner-2"]
    for entry in entries:
        assert isinstance(entry, dict)
        assert str(entry["address"]).startswith("sccpt1")
        keyfile = Path(str(entry["keyfile"]))
        assert keyfile.stat().st_mode & 0o777 == 0o600
        secret_file = Path(str(entry["nonce_file"]))
        assert secret_file.stat().st_mode & 0o777 == 0o600
        assert secret_file.read_text(encoding="utf-8").strip() == entry["nonce_secret"]
    passphrase = stack / "passphrase"
    assert passphrase.stat().st_mode & 0o777 == 0o600


def test_the_stack_builds_a_matchable_chain_and_relay_config(tmp_path: Path) -> None:
    write_stack(tmp_path, passphrase="test-devnet-passphrase")
    miners = bootstrap.load_stack(tmp_path)
    assert [miner.name for miner in miners] == ["miner-1", "miner-2"]

    service, blocks, relay = bootstrap.build_chain(miners)
    try:
        assert service.height == bootstrap.BOOTSTRAP_HEIGHT == 3
        assert bootstrap_end_height(DEVNET) == bootstrap.BOOTSTRAP_HEIGHT
        assert [len(block.transactions) for block in blocks] == [6, 4, 4]
        assert all(not block.proofs for block in blocks)
        assert {tx.type for block in blocks for tx in block.transactions} <= {
            TxType.TRANSFER,
            TxType.REGISTER,
            TxType.AVAILABILITY,
            TxType.REVEAL,
        }

        state = service.chain.state
        record = state.epochs[bootstrap.BOOTSTRAP_EPOCH]
        assert record.matchable
        assert set(record.roster) == {miner.address for miner in miners}
        assert set(state.availability[5]) == {miner.address for miner in miners}

        assert relay["network"] == "devnet"
        beacon = relay["beacon"]
        assert isinstance(beacon, dict)
        assert beacon["epoch"] == bootstrap.BOOTSTRAP_EPOCH
        roster = relay["roster"]
        assert isinstance(roster, dict)
        assert set(roster) == {miner.address for miner in miners}
        for entry in roster.values():
            assert entry["max_games"] == DEVNET.rounds_per_epoch
            assert entry["engine"].startswith("mock-v1:")
            assert entry["public_key"]
        assert relay["open_round"] == 0
    finally:
        service.close()


def test_the_relay_accepts_the_written_config(tmp_path: Path) -> None:
    write_stack(tmp_path, passphrase="test-devnet-passphrase")
    miners = bootstrap.load_stack(tmp_path)
    service, _blocks, relay = bootstrap.build_chain(miners)
    try:
        relay_out = tmp_path / "relay.json"
        bootstrap.write_relay(relay, relay_out)
        document = json.loads(relay_out.read_text(encoding="utf-8"))
        matchmaker = Matchmaker.from_document(document, params=DEVNET)
        assert matchmaker.beacon.epoch == bootstrap.BOOTSTRAP_EPOCH
        assert set(matchmaker.roster) == {miner.address for miner in miners}
    finally:
        service.close()


def test_the_chain_file_is_reopened_and_left_untouched(tmp_path: Path) -> None:
    write_stack(tmp_path, passphrase="test-devnet-passphrase")
    miners = bootstrap.load_stack(tmp_path)
    data_dir = tmp_path / "chain"

    service, blocks, _relay = bootstrap.build_chain(miners, data_dir=data_dir)
    tip = service.tip_hash
    service.close()
    assert len(blocks) == bootstrap.BOOTSTRAP_HEIGHT

    reopened = NodeService.open(DEVNET, storage=Storage(data_dir / "chain.sqlite"))
    try:
        assert reopened.height == bootstrap.BOOTSTRAP_HEIGHT
        assert reopened.tip_hash == tip
    finally:
        reopened.close()

    again, second_blocks, _relay = bootstrap.build_chain(miners, data_dir=data_dir)
    try:
        assert second_blocks == ()
        assert again.height == bootstrap.BOOTSTRAP_HEIGHT
    finally:
        again.close()


def test_main_prints_the_next_steps_and_fresh_rebuilds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    write_stack(tmp_path, passphrase="test-devnet-passphrase")
    monkeypatch.setattr(sys, "argv", ["bootstrap_devnet_stack.py", "--stack", str(tmp_path)])
    bootstrap.main()
    first = capsys.readouterr().out
    assert "built 3 bootstrap blocks" in first
    assert f"relay config for epoch {bootstrap.BOOTSTRAP_EPOCH} written to" in first
    assert f"sccp --network devnet --data-dir {tmp_path / 'chain'} node run --mine" in first
    assert (tmp_path / "relay.json").is_file()

    bootstrap.main()
    second = capsys.readouterr().out
    assert "chain already bootstrapped at height 3" in second

    monkeypatch.setattr(
        sys, "argv", ["bootstrap_devnet_stack.py", "--stack", str(tmp_path), "--fresh"]
    )
    bootstrap.main()
    fresh = capsys.readouterr().out
    assert "built 3 bootstrap blocks: height 3" in fresh


def test_load_stack_rejects_a_testnet_stack(tmp_path: Path) -> None:
    write_stack(tmp_path, passphrase="test-devnet-passphrase")
    summary = json.loads((tmp_path / "stack.json").read_text(encoding="utf-8"))
    summary["network"] = "testnet"
    (tmp_path / "stack.json").write_text(json.dumps(summary), encoding="utf-8")
    with pytest.raises(bootstrap.BootstrapError, match="not a devnet stack"):
        bootstrap.load_stack(tmp_path)
