"""The deterministic testnet stack bootstrap."""

from __future__ import annotations

import sys
from itertools import pairwise
from pathlib import Path
from typing import Any

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
    assert len(entries) == 4
    assert [entry["variant"] for entry in entries] == [
        "standard",
        "standard",
        "chess960",
        "chess960",
    ]
    for entry in entries:
        assert isinstance(entry, dict)
        secret = str(entry["nonce_secret"])
        assert len(bytes.fromhex(secret)) == 32
        path = Path(str(entry["nonce_file"]))
        assert path == tmp_path / "keys" / f"{entry['name']}.nonce"
        assert path.read_text(encoding="utf-8").strip() == secret
        assert path.stat().st_mode & 0o777 == 0o600


def test_the_stack_writes_three_clock_witnesses(tmp_path: Path) -> None:
    """Three witnesses: the relay and two independent services, all registered.

    Their hybrid public keys go to files: 3 970 hex characters is not a sane
    command-line argument, and every miner needs the same bytes.
    """
    summary = write_stack(tmp_path, passphrase="test-stack-passphrase")
    entries = summary["witnesses"]
    assert isinstance(entries, list)
    assert [entry["name"] for entry in entries] == ["witness-1", "witness-2", "witness-3"]
    assert [entry["endpoint"] for entry in entries] == [
        "http://relay:8646",
        "http://witness-2:8647",
        "http://witness-3:8648",
    ]
    for entry in entries:
        assert isinstance(entry, dict)
        keyfile = Path(str(entry["keyfile"]))
        assert keyfile.is_file()
        public_file = Path(str(entry["public_key_file"]))
        assert public_file.read_text(encoding="utf-8").strip() == entry["public_key"]
        assert public_file.stat().st_mode & 0o777 == 0o644
        assert len(str(entry["public_key"])) == 2 * 1985


def test_the_bootstrap_registers_every_witness_on_chain(tmp_path: Path) -> None:
    write_stack(tmp_path, passphrase="test-stack-passphrase")
    miners = bootstrap.load_stack(tmp_path)
    witnesses = bootstrap.load_witnesses(tmp_path)
    assert [witness.name for witness in witnesses] == ["witness-1", "witness-2", "witness-3"]

    service, blocks, relay = bootstrap.build_chain(miners, engine="ab" * 32, witnesses=witnesses)
    try:
        registered = service.chain.state.witnesses
        assert set(registered) == {witness.address for witness in witnesses}
        for witness in witnesses:
            record = registered[witness.address]
            assert record.public_key == witness.public_key
            assert record.endpoint == witness.endpoint
            assert record.bond == bootstrap.TESTNET.clock_witness_bond
        # Block 4 is the registration block; block 1 funded them first.
        assert [tx.type for tx in blocks[3].transactions] == [TxType.REGISTER_WITNESS] * len(
            witnesses
        )
        assert TxType.TRANSFER in {tx.type for tx in blocks[0].transactions}
        published = relay["witnesses"]
        assert isinstance(published, dict)
        assert set(published) == {witness.address for witness in witnesses}
    finally:
        service.close()


def test_a_stack_without_witnesses_still_bootstraps(tmp_path: Path) -> None:
    """The role is additive: an older stack.json keeps working, unattested."""
    summary = write_stack(tmp_path, passphrase="test-stack-passphrase")
    document = tmp_path / "stack.json"
    payload = document.read_text(encoding="utf-8")
    document.write_text(payload.replace('"witnesses"', '"legacy-witnesses"'), encoding="utf-8")
    assert summary["witnesses"]
    assert bootstrap.load_witnesses(tmp_path) == ()
    miners = bootstrap.load_stack(tmp_path)
    service, _blocks, relay = bootstrap.build_chain(miners, engine="ab" * 32)
    try:
        assert service.chain.state.witnesses == {}
        assert "witnesses" not in relay
    finally:
        service.close()


def test_a_rerun_keeps_the_published_relay_config(
    tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    """A second bootstrap must not repoint the relay at a chain that never existed.

    ``build_chain`` dates its blocks from the wall clock, so re-running it on an
    already-bootstrapped node produces *different* beacons. Writing that config
    over the live one makes every proof fail with "beacon did not select this
    opening line".
    """
    write_stack(tmp_path, passphrase="test-stack-passphrase")
    engine = tmp_path / "engine"
    engine.write_bytes(b"not really an engine")
    relay = tmp_path / "relay.json"
    relay.write_text('{"kept": true}\n', encoding="utf-8")

    monkeypatch.setattr(
        bootstrap,
        "_node_height",
        lambda client, wait_seconds=0: bootstrap.BOOTSTRAP_HEIGHT,
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "bootstrap_testnet_stack",
            "--stack",
            str(tmp_path),
            "--node",
            "http://node",
            "--engine-path",
            str(engine),
        ],
    )
    bootstrap.main()

    assert relay.read_text(encoding="utf-8").strip() == '{"kept": true}'
    assert "relay config kept" in capsys.readouterr().out


def test_the_stack_builds_a_matchable_chain_and_relay_config(tmp_path: Path) -> None:
    write_stack(tmp_path, passphrase="test-stack-passphrase")
    miners = bootstrap.load_stack(tmp_path)
    assert [miner.name for miner in miners] == [
        "miner-1",
        "miner-2",
        "miner-3",
        "miner-4",
    ]
    assert [miner.variant for miner in miners] == [
        "standard",
        "standard",
        "chess960",
        "chess960",
    ]
    assert [miner.address for miner in miners] == [
        "sccpt1q87tje79rytrvlasq2g4g86c09yzalk3g4hlepzkz7p95hdlemdxgwqrd9g",
        "sccpt1q9l8ptm8fmdrsmnj80lgf76k8xqrwhpdj0dq6puawdz79c6d8dtswkjp0sd",
        "sccpt1q8samj38z8kxxfng9skcjl4hrmn9cgdknwrr6qvczyy6n20umj6j5v6yh5f",
        "sccpt1qychc3gehqnr0vf5a2ga845tx8pulms4mtddyxplmflfxp8x4txxuqgt37c",
    ]

    engine = "ab" * 32
    service, blocks, relay = bootstrap.build_chain(miners, engine=engine)
    try:
        assert service.height == bootstrap.BOOTSTRAP_HEIGHT == 19
        assert len(blocks) == bootstrap.BOOTSTRAP_HEIGHT
        assert [len(block.transactions) for block in blocks[1:3]] == [4, 4]
        assert all(not block.proofs for block in blocks)

        record = service.chain.state.epochs[bootstrap.BOOTSTRAP_EPOCH]
        assert set(record.roster) == {miner.address for miner in miners}
        assert {member.engine for member in record.roster.values()} == {engine}
        assert [record.roster[miner.address].variant for miner in miners] == [
            "standard",
            "standard",
            "chess960",
            "chess960",
        ]
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
    where a block may skip its proof. A small roster plays fewer games per
    epoch than the epoch has blocks, so a block that waits for a proof never
    comes: no proof is produced, so no block is added, so the availability and
    reveal transactions that build the next roster are never mined.

    Heartbeat blocks break the deadlock. Each carries exactly one pending
    transaction and no subsidy, so the five blocks up to height 24 are crossed
    with no proof at all. Folding those transactions into one block instead
    would still freeze the chain at 24; the epoch-bound availability and reveal
    transactions that no longer fit are pruned at the boundary.
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
        assert len(pending_ids) == 10

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
        assert len(missing) <= 5, "only epoch-bound transactions may be pruned"
        for block in blocks:
            coinbase = block.coinbase
            assert coinbase is not None
            assert coinbase.payload["amount"] == block.transactions[0].fee

        assert service.height == 19 + len(blocks)
        assert service.chain.state.current_epoch() == 4
        assert all(
            tx.type in (TxType.AVAILABILITY, TxType.REVEAL)
            for tx in pending_txs
            if txid(tx) in missing
        ), "only epoch-bound transactions may be pruned at a boundary"
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


def test_bootstrap_blocks_end_at_the_wall_clock(tmp_path: Path) -> None:
    """The setup window must be dated from the deploy, and must end there.

    Two failure modes to keep apart. ``genesis/testnet.json`` is stamped
    2023-11-14, and walking forward from it dated a fresh stack's first twenty
    blocks "in 2023": the explorer labelled them "1095d ago" and the analysis
    notebook's time axes spanned three years. Stamping block 1 at the wall clock
    instead put the *last* block a full window (nine minutes on testnet) into the
    future, and the node's settler paces itself with ``clock() - tip_timestamp >=
    mine_interval``, so no real block could be built until the clock caught up: a
    fresh deploy sat at height 19 with registrations and proofs queued in the
    mempool. The window is now backdated to end at the wall clock.
    """
    write_stack(tmp_path, passphrase="test-stack-passphrase")
    miners = bootstrap.load_stack(tmp_path)
    service, blocks, _relay = bootstrap.build_chain(miners, engine="ab" * 32)
    try:
        stamps = [block.header.timestamp for block in blocks]
        assert len(stamps) == bootstrap.BOOTSTRAP_HEIGHT
        assert min(stamps) > service.chain.genesis_block.header.timestamp, (
            "the bootstrap must not be dated from the 2023 genesis stamp"
        )
        assert stamps == sorted(stamps), "timestamps must stay monotonic"
        gaps = [later - earlier for earlier, later in pairwise(stamps)]
        assert min(gaps) >= service.params.target_block_seconds - 1
        assert abs(stamps[-1] - unix_now()) <= 2 * service.params.target_block_seconds, (
            "the bootstrap window must end at the clock, not run a window ahead of it"
        )
        assert service.chain.genesis_block.header.timestamp == 1_700_000_000, (
            "the genesis stamp is consensus data and must not move"
        )
    finally:
        service.close()
