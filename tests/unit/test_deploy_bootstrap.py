"""The deterministic testnet stack bootstrap."""

from __future__ import annotations

from pathlib import Path

from deploy import bootstrap_testnet_stack as bootstrap
from deploy.init_testnet_stack import write_stack

from sccp.chain.genesis import zero_address
from sccp.chain.tx import txid
from sccp.config import coins_to_units
from sccp.miner.block_builder import BlockBuilder
from sccp.protocol.models import Transaction
from sccp.wallet.service import WalletService


def test_the_stack_publishes_an_availability_secret_per_miner(tmp_path: Path) -> None:
    summary = write_stack(tmp_path, passphrase="test-stack-passphrase")
    entries = summary["miners"]
    assert isinstance(entries, list)
    assert len(entries) == 2
    for entry in entries:
        assert isinstance(entry, dict)
        secret = str(entry["nonce_secret"])
        assert len(bytes.fromhex(secret)) == 32
        path = Path(str(entry["nonce_file"]))
        assert path == tmp_path / "keys" / f"{entry['name']}.nonce"
        assert path.read_text(encoding="utf-8").strip() == secret
        assert path.stat().st_mode & 0o777 == 0o600


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


def test_heartbeat_blocks_carry_a_proofless_epoch_across(tmp_path: Path) -> None:
    """The freeze this whole change is about, in one test.

    Bootstrap leaves the chain at the last height of epoch 3, past the point
    where a block may skip its proof. A two-miner roster plays fewer games per
    epoch than the epoch has blocks, so a block that waits for a proof never
    comes: no proof is produced, so no block is added, so the availability and
    reveal transactions that build the next roster are never mined.

    Heartbeat blocks break the deadlock. Each carries exactly one pending
    transaction and no subsidy, so six pending transactions buy six blocks and
    the boundary at height 24 is crossed with no proof at all. Folding those six
    transactions into one block instead would still freeze the chain at 24.
    """
    write_stack(tmp_path, passphrase="test-stack-passphrase")
    miners = bootstrap.load_stack(tmp_path)
    service, _blocks, _relay = bootstrap.build_chain(miners, engine="ab" * 32)
    try:
        params = service.params
        assert service.height == bootstrap.BOOTSTRAP_HEIGHT

        def submit(tx: Transaction) -> None:
            service.submit_transaction(tx)

        def wallet() -> WalletService:
            return WalletService(params, service.mempool.projection)

        # The two miners commit and reveal for the epochs after the bootstrap,
        # exactly as ``EpochPipeline`` would on their behalf.
        for miner in miners:
            submit(
                wallet().build_availability(
                    miner.keypair,
                    nonce_secret=miner.nonce_secret,
                    max_games=params.rounds_per_epoch,
                )
            )
            submit(wallet().build_reveal(miner.keypair, miner.nonce_secret))
        # Ordinary ledger work, so the test does not depend on a game finishing.
        for _ in range(2):
            submit(wallet().build_transfer(miners[0].keypair, miners[1].address, coins_to_units(1)))
        pending = set(service.mempool.transactions)
        assert len(pending) == 6

        blocks = []
        for _ in range(len(pending)):
            block = BlockBuilder(
                params=params,
                chain=service.chain,
                mempool=service.mempool,
                miner_address=zero_address(params),
            ).build(timestamp=service.chain.state.timestamps[-1] + params.target_block_seconds)
            assert block is not None
            assert service.ingest_block(block).accepted
            blocks.append(block)

        assert [block.header.height for block in blocks] == [20, 21, 22, 23, 24, 25]
        assert all(block.proofs == () for block in blocks)
        included = [tx for block in blocks for tx in block.transactions]
        assert len(included) == len(blocks) == 6, "one transaction per heartbeat block"
        assert {txid(tx) for tx in included} == pending
        for block in blocks:
            coinbase = block.coinbase
            assert coinbase is not None
            assert coinbase.payload["amount"] == block.transactions[0].fee

        assert service.height == 25
        assert service.chain.state.current_epoch() == 5
        # The pool is empty, so another miner finds nothing to build without a
        # proof: heartbeats restart the chain, they do not pad it.
        assert service.mempool.transactions == {}
        assert (
            BlockBuilder(
                params=params,
                chain=service.chain,
                mempool=service.mempool,
                miner_address=zero_address(params),
            ).build(timestamp=service.chain.state.timestamps[-1] + params.target_block_seconds)
            is None
        )
    finally:
        service.close()
