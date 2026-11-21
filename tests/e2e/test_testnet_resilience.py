"""Testnet resilience drills: restart, partition, reorg, pressure and replay.

``tests/e2e/test_local_network.py`` proves the transport works and that an
offline node can catch up. These drills cover the failure modes an operator
actually meets on a long-lived testnet:

* a node or relay process restarts while work is in flight;
* two nodes mine through a partition and have to agree on one branch again;
* a heavier branch reorganises the tip and the orphaned transactions must
  return to the mempool without being replayed twice;
* SQLite reports a full disk or a read-only volume;
* a peer's wall clock is ahead of, or behind, the chain's median time;
* a replayed block, transaction or relay receipt must be idempotent; and
* the pools and gossip cache stay inside their configured bounds.

Everything here drives the public service/API objects. The only private access
is the storage connection used to make SQLite report ``SQLITE_FULL`` without
filling the developer's disk.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from sccp.chain.block import MAX_FUTURE_SECONDS, BlockFailure, block_hash, block_size
from sccp.chain.chain import Chain
from sccp.chain.genesis import faucet_keypair
from sccp.chain.mempool import Mempool
from sccp.chain.storage import Storage, persist_chain
from sccp.chain.tx import tx_size, txid
from sccp.config import DEVNET, UNITS_PER_COIN
from sccp.crypto.address import encode_address
from sccp.crypto.pq import (
    generate_hybrid_keypair,
    hybrid_keypair_from_seed,
    hybrid_sign_domain,
)
from sccp.errors import BlockValidationError, MempoolError
from sccp.miner.block_builder import BlockBuilder
from sccp.miner.matchmaker import Matchmaker
from sccp.miner.matchmaker import create_app as create_relay_app
from sccp.node.app import create_app as create_node_app
from sccp.node.p2p import (
    BAN_SECONDS,
    MAX_PEER_FAILURES,
    GossipCache,
    GossipKind,
    PeerTable,
    sign_envelope,
)
from sccp.node.service import HttpPeerClient, NodeService
from sccp.protocol.clocks import sign_move
from sccp.protocol.models import MoveMessage
from sccp.protocol.transcript import certificate_payload
from sccp.serialization import Domain
from sccp.transport import client_for
from sccp.wallet.service import WalletService

MINER = hybrid_keypair_from_seed(bytes([9]) * 32)
MINER_ADDRESS = encode_address(MINER.public_key_bytes, DEVNET)
FAUCET = faucet_keypair("devnet")
FAUCET_ADDRESS = encode_address(FAUCET.public_key_bytes, DEVNET)

RELAY_SECONDS = 1_700_000_000
CONTINUATION = (("white", "Qh5"), ("black", "Nf6"))


def _json(model: Any) -> dict[str, Any]:
    """A pydantic model as the wire document the HTTP routes accept."""
    return model.model_dump(mode="json", by_alias=True)


def _build(
    chain: Chain,
    *,
    mempool: Mempool | None = None,
    txs: tuple[Any, ...] = (),
    timestamp: int | None = None,
) -> Any:
    """Build the next block for ``chain`` without connecting it."""
    pool = (
        mempool
        if mempool is not None
        else Mempool(params=chain.params, tip_state=chain.state, book=chain.book)
    )
    for tx in txs:
        pool.add_transaction(tx)
    stamp = chain.state.timestamps[-1] + 1 if timestamp is None else timestamp
    block = BlockBuilder(params=chain.params, chain=chain, mempool=pool, keypair=MINER).build(
        timestamp=stamp
    )
    assert block is not None, "the builder refused to produce a block"
    return block


def _fork(chain: Chain) -> Chain:
    """An empty chain with the same genesis, for mining a side branch."""
    return Chain(
        params=chain.params,
        genesis_block=chain.genesis_block,
        genesis_state=chain.genesis_state,
        book=chain.book,
    )


def _service(chain: Chain) -> NodeService:
    """A node around an existing chain, with a pool rebased on its tip."""
    return NodeService(
        params=chain.params,
        chain=chain,
        mempool=Mempool(params=chain.params, tip_state=chain.state, book=chain.book),
    )


def _post_block(client: Any, block: Any) -> Any:
    """Submit a block through the same route a peer would use."""
    return client.post("/v1/blocks", json=_json(block))


# --- relay helpers (kept local so this module owns the whole scenario) -------


@dataclass
class FakeClock:
    """One adjustable wall clock and monotonic millisecond clock."""

    seconds: int = RELAY_SECONDS
    ms: int = 5_000_000

    def now_seconds(self) -> int:
        return self.seconds

    def now_ms(self) -> int:
        return self.ms

    def advance(self, milliseconds: int) -> None:
        self.ms += milliseconds


def _relay(
    signed_match: Any,
    *,
    store: Path,
    clock: FakeClock,
    witness: Any,
) -> Matchmaker:
    """A relay over the signed match, persisting every session to ``store``."""
    engines = {
        account.address: f"mock-v1:{index:016d}"
        for index, account in enumerate(signed_match.accounts.values(), start=1)
    }
    return Matchmaker(
        signed_match.params,
        beacon=signed_match.beacon,
        roster=signed_match.roster,
        public_keys=signed_match.public_keys,
        engines=engines,
        book=signed_match.book,
        store=store,
        clock=clock.now_seconds,
        clock_ms=clock.now_ms,
        timeout_seconds=300,
        witness_keypair=witness,
    )


def _account_for(signed_match: Any, address: str) -> Any:
    return next(account for account in signed_match.accounts.values() if account.address == address)


def _join_seat(client: TestClient, signed_match: Any, view: dict[str, Any], seat: str) -> dict:
    address = view["certificate"][seat]
    account = _account_for(signed_match, address)
    certificate = signed_match.certificate.__class__.model_validate(view["certificate"])
    signature = hybrid_sign_domain(
        account.keypair,
        Domain.CERTIFICATE,
        certificate_payload(certificate),
    ).hex()
    response = client.post(
        f"/v1/matches/{view['matchup']['match_id']}/join",
        json={
            "seat": address,
            "public_key": account.keypair.public_key_hex,
            "signature": signature,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _start_relay(
    signed_match: Any,
    *,
    store: Path,
    clock: FakeClock,
    witness: Any,
) -> tuple[Matchmaker, TestClient, dict[str, Any]]:
    """Open round 0, join both seats and return the active session."""
    state = _relay(signed_match, store=store, clock=clock, witness=witness)
    client = TestClient(create_relay_app(state))
    assert client.get("/v1/matches").json() == []
    client.post("/v1/rounds", json={"round": 0})
    view = client.get("/v1/matches").json()[0]
    for seat in ("white", "black"):
        view = _join_seat(client, signed_match, view, seat)
    assert view["status"] == "active"
    return state, client, view


def _move_body(
    signed_match: Any,
    view: dict[str, Any],
    seat: str,
    *,
    ply: int,
    san: str,
) -> dict[str, Any]:
    """A seat-signed, clocked move body for the relay's HTTP API."""
    address = view["certificate"][seat]
    account = _account_for(signed_match, address)
    signed = sign_move(
        MoveMessage(
            network=view["certificate"]["network"],
            match_id=view["matchup"]["match_id"],
            seat=address,
            ply=ply,
            san=san,
            clock_ms=0,
            signature="0" * 128,
        ),
        account.keypair,
    )
    return {
        "seat": signed.seat,
        "san": signed.san,
        "network": signed.network,
        "ply": signed.ply,
        "clock_ms": 1_000,
        "signature": signed.signature,
    }


# --- restart ----------------------------------------------------------------


def test_a_node_restart_replays_storage_and_drops_the_volatile_mempool(
    live_chain: Any, tmp_path: Path
) -> None:
    """The chain survives a process restart; the mempool deliberately does not.

    A restart is a replay from storage, not a trust-the-snapshot shortcut. The
    pending transaction is memory-only by design, so a node that comes back must
    accept it again over gossip/HTTP rather than half-apply it from disk.
    """
    path = tmp_path / "node.db"
    storage = Storage(path)
    persist_chain(live_chain.chain, storage)
    service = NodeService.open(DEVNET, storage=storage, book=live_chain.chain.book)
    alice = live_chain.accounts["alice"]
    bob = live_chain.accounts["bob"]
    tx = WalletService(DEVNET, service.mempool.projection).build_transfer(
        alice.keypair, bob.address, 1_000_000
    )
    tip_hash = service.tip_hash
    tip_block = service.chain.tip.block
    try:
        with client_for(create_node_app(service)) as http:
            accepted = http.post("/v1/tx", json=_json(tx))
            assert accepted.status_code == 200, accepted.text
            assert accepted.json()["txid"] == txid(tx)
            before = http.get("/v1/status").json()
            before_account = http.get(f"/v1/accounts/{alice.address}").json()
            before_pool = http.get("/v1/mempool").json()
        assert before_pool["transactions"] == [txid(tx)]
        assert before["mempool"]["transactions"] == 1
    finally:
        service.close()

    reopened = Storage(path)
    restarted = NodeService.open(DEVNET, storage=reopened, book=live_chain.chain.book)
    try:
        # The replayed chain is exactly the one that was durable.
        assert restarted.height == before["height"]
        assert restarted.tip_hash == tip_hash
        assert restarted.status().mempool.transactions == 0
        with client_for(create_node_app(restarted)) as http:
            after = http.get("/v1/status").json()
            assert after["height"] == before["height"]
            assert after["tip_hash"] == tip_hash
            assert after["genesis_hash"] == before["genesis_hash"]
            assert after["mempool"]["transactions"] == 0
            assert http.get(f"/v1/accounts/{alice.address}").json() == before_account
            assert http.get(f"/v1/blocks/{tip_hash}").json()["hash"] == tip_hash

            # Replaying the same signed transaction repopulates the pool.
            replayed = http.post("/v1/tx", json=_json(tx))
            assert replayed.status_code == 200, replayed.text
            assert http.get("/v1/mempool").json()["transactions"] == [txid(tx)]

            # Replaying the tip block is accepted as a duplicate, not a new block.
            duplicate = _post_block(http, tip_block)
            assert duplicate.status_code == 200, duplicate.text
            assert duplicate.json()["hash"] == tip_hash
            assert http.get("/v1/status").json()["height"] == before["height"]
    finally:
        restarted.close()


def test_a_relay_restart_replays_receipts_without_double_charging(
    signed_match: Any, tmp_path: Path
) -> None:
    """A restarted relay re-serves the same receipts and refuses replayed moves.

    The relay's JSON store is the durable match log. Restarting the process must
    not create a second receipt for a ply it already accepted, must not charge a
    seat again for the same move, and must re-base the live clock rather than
    treating six lost hours as time on the players' clocks.
    """
    store = tmp_path / "relay.json"
    clock = FakeClock()
    witness = generate_hybrid_keypair()
    _state, client, view = _start_relay(signed_match, store=store, clock=clock, witness=witness)
    match_id = view["matchup"]["match_id"]
    ply = len(view["san_moves"])
    clock.advance(4_000)
    body = _move_body(signed_match, view, "white", ply=ply, san=CONTINUATION[0][1])
    played = client.post(f"/v1/matches/{match_id}/move", json=body)
    assert played.status_code == 200, played.text
    moved = played.json()
    assert moved["receipts"] == 1
    bundle = client.get(f"/v1/matches/{match_id}/receipts").json()
    assert len(bundle["receipts"]) == 1
    first_receipt = bundle["receipts"][0]
    assert first_receipt["ply"] == ply
    assert first_receipt["san"] == CONTINUATION[0][1]
    assert first_receipt["white_ms"] == moved["white_ms"]

    # Replaying the exact signed body is a duplicate ply, not a second move.
    duplicate = client.post(f"/v1/matches/{match_id}/move", json=body)
    assert duplicate.status_code == 400
    unchanged = client.get(f"/v1/matches/{match_id}").json()
    assert unchanged["receipts"] == 1
    assert unchanged["white_ms"] == moved["white_ms"]
    assert client.get(f"/v1/matches/{match_id}/receipts").json() == bundle

    # A new process loads the store after a long wall-clock jump. Receipts are
    # durable; the monotonic clock is not, so the turn restarts at "now".
    later = FakeClock(
        seconds=clock.seconds + 6 * 60 * 60,
        ms=clock.ms + 6 * 60 * 60 * 1_000,
    )
    restarted = _relay(signed_match, store=store, clock=later, witness=witness)
    second = TestClient(create_relay_app(restarted))
    resumed = second.get(f"/v1/matches/{match_id}").json()
    assert resumed["status"] == "active"
    assert resumed["san_moves"] == unchanged["san_moves"]
    assert resumed["receipts"] == 1
    assert second.get(f"/v1/matches/{match_id}/receipts").json()["receipts"] == bundle["receipts"]

    # The old body is still refused by the new process.
    stale = second.post(f"/v1/matches/{match_id}/move", json=body)
    assert stale.status_code == 400

    # The next seat's move appends one receipt and leaves the first untouched.
    next_ply = len(resumed["san_moves"])
    black = _move_body(
        signed_match,
        resumed,
        "black",
        ply=next_ply,
        san=CONTINUATION[1][1],
    )
    continued = second.post(f"/v1/matches/{match_id}/move", json=black)
    assert continued.status_code == 200, continued.text
    fresh = second.get(f"/v1/matches/{match_id}/receipts").json()
    assert len(fresh["receipts"]) == 2
    assert fresh["receipts"][0] == first_receipt
    assert fresh["receipts"][1]["ply"] == next_ply
    assert fresh["receipts"][1]["san"] == CONTINUATION[1][1]


# --- partition / heal -------------------------------------------------------


def test_a_partition_heals_when_a_heavier_side_branch_is_relayed(devnet_chain: Any) -> None:
    """Two nodes mine through a partition; relaying blocks converges them.

    Each node mines its own branch while disconnected. Healing is not a simple
    height catch-up: the node with the shorter branch has to store the other
    branch's first block as a side branch and adopt the heavier tip. The final
    pull-sync must then have nothing left to request.
    """
    first = _service(_fork(devnet_chain))
    second = _service(_fork(devnet_chain))
    origin = devnet_chain.state.timestamps[-1]
    try:
        first_block = _build(first.chain, timestamp=origin + 1)
        assert first.ingest_block(first_block).accepted
        assert first.height == 1
        assert second.height == 0

        # The other side of the partition mined two blocks, not one. Distinct
        # timestamps make the first blocks genuinely different, rather than the
        # same deterministic block assembled twice.
        second_one = _build(second.chain, timestamp=origin + 10)
        assert second.ingest_block(second_one).accepted
        second_two = _build(second.chain, timestamp=origin + 11)
        assert second.ingest_block(second_two).accepted
        assert second.height == 2
        assert first.tip_hash != second.tip_hash

        # Heal by relaying the heavier branch's blocks over the block route.
        with client_for(create_node_app(first)) as first_http:
            for block in (second_one, second_two):
                response = _post_block(first_http, block)
                assert response.status_code == 200, response.text
        assert first.height == 2
        assert first.tip_hash == second.tip_hash == block_hash(second_two.header)
        assert first.chain.is_main_chain(block_hash(first_block.header)) is False

        # The other direction keeps the losing first block as a known side branch.
        with client_for(create_node_app(second)) as second_http:
            response = _post_block(second_http, first_block)
            assert response.status_code == 200, response.text
            assert second.tip_hash == block_hash(second_two.header)
            assert second.chain.get(block_hash(first_block.header)) is not None

            # Nothing is missing once both sides have seen the same blocks.
            peer = HttpPeerClient("http://second", client=second_http)
            report = first.sync_from(peer, url="http://second")
        assert report.requested == 0
        assert report.adopted == 0
        assert first.tip_hash == second.tip_hash
    finally:
        first.close()
        second.close()


# --- reorg / mempool interaction -------------------------------------------


def test_a_reorg_returns_orphaned_transactions_without_replaying_them(
    devnet_chain: Any, protocol_accounts: Any
) -> None:
    """A heavier branch evicts dependants and re-queues the orphaned ancestor.

    The funding transaction in the discarded block must return to the pool. A
    transaction that depended on it is *not* silently kept against a tip where
    its input never existed; the wallet rebuilds and resubmits it. Settling the
    recovered pool then applies every transaction exactly once.
    """
    alice = protocol_accounts["alice"]
    bob = protocol_accounts["bob"]
    node = _service(_fork(devnet_chain))
    try:
        funding = WalletService(node.params, node.chain.state).build_transfer(
            FAUCET, alice.address, 10 * UNITS_PER_COIN
        )
        discarded = _build(node.chain, txs=(funding,))
        assert node.ingest_block(discarded).accepted
        assert node.height == 1
        assert node.mempool.pending_transaction_count == 0

        # Both pending transactions are valid against the adopted block's tip.
        alice_tx = WalletService(node.params, node.mempool.projection).build_transfer(
            alice.keypair, bob.address, UNITS_PER_COIN
        )
        node.submit_transaction(alice_tx)
        faucet_tx = WalletService(node.params, node.mempool.projection).build_transfer(
            FAUCET, bob.address, UNITS_PER_COIN
        )
        node.submit_transaction(faucet_tx)
        assert node.mempool.pending_transaction_count == 2

        # While disconnected the other branch mined two empty blocks.
        side = _fork(node.chain)
        side_one = _build(side)
        side.add_block(side_one)
        side_two = _build(side)
        side.add_block(side_two)

        outcomes = [node.ingest_block(side_one), node.ingest_block(side_two)]
        assert any(outcome.reorged for outcome in outcomes)
        assert node.height == 2
        assert node.tip_hash == block_hash(side_two.header)
        assert node.chain.is_main_chain(block_hash(discarded.header)) is False

        # The orphaned ancestor is back; its dependants were not kept against a
        # tip where the funding transaction does not exist.
        assert node.mempool.pending_transaction_count == 1
        assert node.mempool.has_transaction(txid(funding))
        assert not node.mempool.has_transaction(txid(alice_tx))
        assert not node.mempool.has_transaction(txid(faucet_tx))

        # Rebuilding against the new projection recovers both dependants.
        replacement_alice = WalletService(node.params, node.mempool.projection).build_transfer(
            alice.keypair, bob.address, UNITS_PER_COIN
        )
        replacement_faucet = WalletService(node.params, node.mempool.projection).build_transfer(
            FAUCET, bob.address, UNITS_PER_COIN
        )
        node.submit_transaction(replacement_alice)
        node.submit_transaction(replacement_faucet)
        assert node.mempool.pending_transaction_count == 3

        settlement = _build(node.chain, mempool=node.mempool)
        assert {txid(tx) for tx in settlement.transactions} == {
            txid(funding),
            txid(replacement_alice),
            txid(replacement_faucet),
        }
        assert block_size(settlement) <= node.params.max_block_bytes
        assert node.ingest_block(settlement).accepted
        assert node.height == 3
        assert node.mempool.pending_transaction_count == 0

        # Every transfer landed once: faucet used nonces 0 and 1, Alice once.
        assert node.chain.state.account(FAUCET_ADDRESS).nonce == 2
        assert node.chain.state.account(alice.address).nonce == 1
        assert node.chain.state.account(bob.address).balance == 2 * UNITS_PER_COIN
    finally:
        node.close()


# --- disk pressure ----------------------------------------------------------


def test_disk_pressure_keeps_the_durable_tip_and_recovers_on_restart(tmp_path: Path) -> None:
    """A failed durable write is retried from the last tip after a restart.

    SQLite is made to report ``SQLITE_FULL`` on a real insert, then the node's
    volume is made read-only. In both cases the durable tip must stay at the last
    successful block. The in-memory chain may have accepted the block before the
    write failed, but a restart must replay to the durable tip and the block can
    then be re-ingested once the volume is writable again.
    """
    path = tmp_path / "pressure.db"
    service = NodeService.open(DEVNET, storage=Storage(path))
    try:
        block_one = _build(service.chain)
        assert service.ingest_block(block_one).accepted
        storage = service.storage
        assert storage is not None
        first_hash = block_hash(block_one.header)

        # A real SQLite full disk: no page growth is allowed, so a large insert
        # fails with SQLITE_FULL. The failed statement must not advance the tip.
        storage._connection.execute("CREATE TABLE pressure (payload BLOB)")
        pages = int(storage._connection.execute("PRAGMA page_count").fetchone()[0])
        storage._connection.execute(f"PRAGMA max_page_count={pages}")
        with pytest.raises(sqlite3.OperationalError, match="disk is full"):
            storage._connection.execute("INSERT INTO pressure VALUES (zeroblob(4000000))")
        storage._connection.execute("PRAGMA max_page_count=1048576")
        storage._connection.execute("DROP TABLE pressure")
        assert storage.tip_hash() == first_hash
        assert storage.block_count() == 1

        # A read-only volume fails the next block's durable write.
        block_two = _build(service.chain)
        storage._connection.execute("PRAGMA query_only=ON")
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            service.ingest_block(block_two)
        assert service.height == 2  # the in-memory tip did move
        assert service.tip_hash == block_hash(block_two.header)
        assert storage.tip_hash() == first_hash  # the durable tip did not
        assert storage.block_count() == 1
        recovered_root = service.chain.state.state_root()
    finally:
        service.close()

    # A fresh process has a writable connection and replays the durable chain.
    restarted = NodeService.open(DEVNET, storage=Storage(path))
    try:
        assert restarted.height == 1
        assert restarted.tip_hash == first_hash
        assert restarted.chain.state.state_root() != recovered_root
        result = restarted.ingest_block(block_two)
        assert result.accepted and not result.duplicate
        assert restarted.height == 2
        assert restarted.tip_hash == block_hash(block_two.header)
        assert restarted.chain.state.state_root() == recovered_root
        assert restarted.storage is not None
        assert restarted.storage.block_count() == 2
    finally:
        restarted.close()


# --- clock skew -------------------------------------------------------------


def test_clock_skew_is_bounded_by_the_future_and_median_time_rules() -> None:
    """A peer clock cannot push the chain forward or drag it backward.

    The two-hour future bound is checked with the verifier's own clock, while the
    median-time-past rule makes an old timestamp inadmissible even when no
    verifier clock is supplied. A rejected block leaves no orphan behind.
    """
    node = NodeService.open(DEVNET)
    now = DEVNET.genesis_timestamp
    assert now is not None
    try:
        far_future = _build(node.chain, timestamp=now + MAX_FUTURE_SECONDS + 1)
        with pytest.raises(BlockValidationError) as rejected:
            node.ingest_block(far_future, now=now)
        assert rejected.value.reason == BlockFailure.TIMESTAMP
        assert "future" in str(rejected.value)
        assert node.height == 0
        assert node.chain.has(block_hash(far_future.header)) is False
        assert node.chain.pending_orphans() == 0

        # Exactly at the bound is admissible; one second later is not.
        at_bound = _build(node.chain, timestamp=now + MAX_FUTURE_SECONDS)
        assert node.ingest_block(at_bound, now=now).accepted
        assert node.height == 1

        with client_for(create_node_app(node)) as http:
            stale = _build(node.chain, timestamp=now)
            old = _post_block(http, stale)
            assert old.status_code == 400, old.text
            assert "median time past" in old.json()["detail"]
            assert node.height == 1
            assert node.chain.is_main_chain(block_hash(stale.header)) is False

            # A normal next block still follows the adopted future timestamp.
            following = _build(node.chain, timestamp=now + MAX_FUTURE_SECONDS + 1)
            accepted = _post_block(http, following)
            assert accepted.status_code == 200, accepted.text
            assert accepted.json()["height"] == 2
        assert node.height == 2
        assert node.chain.state.timestamps[-2:] == [
            now + MAX_FUTURE_SECONDS,
            now + MAX_FUTURE_SECONDS + 1,
        ]
    finally:
        node.close()


# --- replay and bounded resources ------------------------------------------


def test_replayed_blocks_transactions_and_gossip_are_idempotent(devnet_chain: Any) -> None:
    """A relayed block or transaction settles once, however many times it arrives.

    Block replay is normal on a gossip network. The second delivery must be
    reported as a duplicate, leave the height alone, and not apply the body a
    second time. A transaction already settled on chain is refused outright.
    """
    node = _service(_fork(devnet_chain))
    try:
        tx = WalletService(node.params, node.mempool.projection).build_transfer(
            FAUCET, MINER_ADDRESS, 5_000
        )
        node.submit_transaction(tx)
        block = _build(node.chain, mempool=node.mempool)
        first = node.ingest_block(block)
        second = node.ingest_block(block)
        assert first.accepted and not first.duplicate
        assert second.accepted and second.duplicate
        assert node.height == 1
        assert node.chain.state.account(FAUCET_ADDRESS).nonce == 1
        assert node.mempool.pending_transaction_count == 0

        # The settled body cannot be admitted to the pool again.
        with pytest.raises(MempoolError, match="nonce"):
            node.submit_transaction(tx)

        # Gossip replay is deduplicated by content id, not by the envelope.
        envelope = sign_envelope(
            MINER,
            network=DEVNET.network_id,
            kind=GossipKind.BLOCK,
            payload={"block": _json(block)},
        )
        announced = node.handle_gossip(envelope, public_key=MINER.public_key_hex)
        replayed = node.handle_gossip(envelope, public_key=MINER.public_key_hex)
        assert announced.duplicate is False
        assert replayed.duplicate is True
        assert announced.object_id == replayed.object_id == block_hash(block.header)
        assert node.height == 1
        assert node.chain.state.account(FAUCET_ADDRESS).nonce == 1
    finally:
        node.close()


def test_bounded_pools_evict_stale_work_and_cap_selection(devnet_chain: Any) -> None:
    """The mempool, gossip cache and peer-abuse table all have hard bounds.

    A testnet that keeps running must not grow these structures without limit:
    stale transactions expire, block selection respects the size budget, the
    dedupe cache evicts its oldest entry, and a failing peer is banned and then
    becomes eligible again after the ban window.
    """
    chain = _fork(devnet_chain)
    pool = Mempool(params=chain.params, tip_state=chain.state, book=chain.book)
    for _ in range(40):
        tx = WalletService(chain.params, pool.projection).build_transfer(
            FAUCET, MINER_ADDRESS, 1_000
        )
        pool.add_transaction(tx)
    assert pool.pending_transaction_count == 40

    entries = list(pool.transactions.values())
    budget = entries[0].size + entries[1].size
    selected = pool.select_transactions(height=3, max_bytes=budget)
    assert len(selected) == 2
    assert sum(tx_size(tx) for tx in selected) <= budget
    assert pool.select_transactions(height=3, max_bytes=entries[0].size - 1) == []

    expired = pool.prune(pool.next_height + chain.params.mempool_ttl_blocks + 1)
    assert len(expired) == 40
    assert pool.pending_transaction_count == 0

    cache = GossipCache(max_entries=3)
    for index in range(4):
        assert cache.check_and_add(GossipKind.TRANSACTION, f"id-{index}") is True
    assert len(cache) == 3
    assert ("transaction", "id-0") not in cache.seen
    assert cache.check_and_add(GossipKind.TRANSACTION, "id-3") is False
    assert len(cache) == 3

    peers = PeerTable()
    for _ in range(MAX_PEER_FAILURES):
        peers.record_failure("http://bad", now=0)
    assert peers.is_banned("http://bad", now=0) is True
    assert peers.eligible(now=0) == []
    assert peers.is_banned("http://bad", now=BAN_SECONDS) is False
    assert [record.url for record in peers.eligible(now=BAN_SECONDS)] == ["http://bad"]
