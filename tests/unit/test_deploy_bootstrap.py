"""The deterministic testnet stack bootstrap (plan M6)."""

from __future__ import annotations

from pathlib import Path

from deploy import bootstrap_testnet_stack as bootstrap
from deploy.init_testnet_stack import write_stack


def test_the_stack_builds_a_matchable_chain_and_relay_config(tmp_path: Path) -> None:
    write_stack(tmp_path, passphrase="test-stack-passphrase")
    miners = bootstrap.load_stack(tmp_path)
    assert [miner.name for miner in miners] == ["miner-1", "miner-2"]
    assert [miner.address for miner in miners] == [
        "sccpt1qrvwy92u2es34fnpwnddkhtpw8wzqjpy6u6yszpz",
        "sccpt1qrrx3yp3ufmfsqk6l9u0wg7xpjd7670v5v85vgp9",
    ]

    engine = "ab" * 32
    service, blocks, relay = bootstrap.build_chain(miners, engine=engine)
    try:
        assert service.height == bootstrap.BOOTSTRAP_HEIGHT == 19
        assert len(blocks) == bootstrap.BOOTSTRAP_HEIGHT
        assert [len(block.transactions) for block in blocks[1:3]] == [2, 2]
        assert all(not block.proofs for block in blocks)

        record = service.chain.state.epochs[bootstrap.BOOTSTRAP_EPOCH]
        assert set(record.roster) == {miner.address for miner in miners}
        assert {member.engine for member in record.roster.values()} == {engine}
        assert record.beacon is not None
        assert record.roster_root

        assert relay["network"] == "testnet"
        assert relay["beacon"] == record.beacon.model_dump(mode="json")
        roster = relay["roster"]
        assert isinstance(roster, dict)
        assert set(roster) == {miner.address for miner in miners}
        assert relay["round_seconds"] > 0
    finally:
        service.close()
