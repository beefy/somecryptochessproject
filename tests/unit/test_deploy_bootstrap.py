"""The deterministic testnet stack bootstrap."""

from __future__ import annotations

from itertools import pairwise
from pathlib import Path

from deploy import bootstrap_testnet_stack as bootstrap
from deploy.init_testnet_stack import write_stack

from sccp.chain.genesis import zero_address
from sccp.chain.tx import txid
from sccp.config import coins_to_units
from sccp.miner.block_builder import BlockBuilder
from sccp.node.service import unix_now
from sccp.protocol.models import Block, Transaction, TxType
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
        "sccpt1q87tje79rytrvlasq2g4g86c09yzalk3g4hlepzkz7p95hdlemdxgwqrd9g",
        "sccpt1q9l8ptm8fmdrsmnj80lgf76k8xqrwhpdj0dq6puawdz79c6d8dtswkjp0sd",
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
        pending_ids = set(service.mempool.transactions)
        pending_txs = [entry.tx for entry in service.mempool.transactions.values()]
        assert len(pending_ids) == 6

        # Heartbeat blocks carry one ordinary transaction each. The chain moves
        # one block per transaction until the pool is empty; a reveal whose
        # epoch has passed is pruned rather than packed into a block that would
        # reject it, so the last few sends may not all make it.
        blocks: list[Block] = []
        included: list[Transaction] = []
        while service.mempool.transactions:
            block = BlockBuilder(
                params=params,
                chain=service.chain,
                mempool=service.mempool,
                miner_address=zero_address(params),
            ).build(timestamp=service.chain.state.timestamps[-1] + params.target_block_seconds)
            assert block is not None, (
                f"empty build at height {service.height + 1} with "
                f"{len(service.mempool.transactions)} pooled"
            )
            assert service.ingest_block(block).accepted
            blocks.append(block)
            included.extend(block.transactions)

        assert [block.header.height for block in blocks] == list(range(20, 20 + len(blocks)))
        assert all(block.proofs == () for block in blocks)
        assert all(len(block.transactions) == 1 for block in blocks), (
            "one transaction per heartbeat block"
        )
        assert {txid(tx) for tx in included} <= pending_ids
        missing = pending_ids - {txid(tx) for tx in included}
        assert len(missing) <= 1, "only an expired reveal may be pruned"
        for block in blocks:
            coinbase = block.coinbase
            assert coinbase is not None
            assert coinbase.payload["amount"] == block.transactions[0].fee

        assert service.height == 19 + len(blocks)
        assert service.chain.state.current_epoch() == 4
        assert all(tx.type is TxType.REVEAL for tx in pending_txs if txid(tx) in missing), (
            "only epoch-bound transactions may be pruned at a boundary"
        )
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


def test_bootstrap_blocks_are_stamped_with_the_wall_clock(tmp_path: Path) -> None:
    """The setup window must not be dated from the genesis constant.

    ``genesis/testnet.json`` is stamped 2023-11-14 and the bootstrap used to walk
    forward from it, so a stack deployed today mined its first twenty blocks
    "in 2023": the explorer labelled them "1095d ago" and the analysis
    notebook's time axes spanned three years, which made throughput read
    zero. Every bootstrap block is now built at max(now, tip + target), so the
    chain starts when it is deployed while the genesis stamp stays untouched.
    """
    write_stack(tmp_path, passphrase="test-stack-passphrase")
    miners = bootstrap.load_stack(tmp_path)
    started = unix_now()
    service, blocks, _relay = bootstrap.build_chain(miners, engine="ab" * 32)
    try:
        stamps = [block.header.timestamp for block in blocks]
        assert len(stamps) == bootstrap.BOOTSTRAP_HEIGHT
        assert min(stamps) >= started, "bootstrap blocks are dated before the deploy"
        assert stamps == sorted(stamps), "timestamps must stay monotonic"
        gaps = [later - earlier for earlier, later in pairwise(stamps)]
        assert min(gaps) >= service.params.target_block_seconds - 1
        assert service.chain.genesis_block.header.timestamp == 1_700_000_000, (
            "the genesis stamp is consensus data and must not move"
        )
    finally:
        service.close()
