"""Gossip envelopes, the peer table, dedupe and pull-sync planning (M4)."""

from __future__ import annotations

import random
from typing import Any

import pytest

from sccp.chain.block import block_hash
from sccp.chain.genesis import faucet_keypair
from sccp.chain.tx import txid
from sccp.config import DEVNET, TESTNET
from sccp.crypto.address import encode_address
from sccp.crypto.keys import keypair_from_seed
from sccp.errors import GossipError
from sccp.node.p2p import (
    BAN_SECONDS,
    MAX_PEER_FAILURES,
    GossipCache,
    GossipKind,
    PeerRecord,
    PeerTable,
    choose_fanout,
    envelope_payload,
    fetch_blocks,
    object_id_of,
    plan_sync,
    sign_envelope,
    verify_envelope,
)
from sccp.wallet.service import WalletService

BOB = keypair_from_seed(bytes([7]) * 32)
BOB_ADDRESS = encode_address(BOB.public_key_bytes, DEVNET)


def test_envelope_roundtrip() -> None:
    envelope = sign_envelope(
        BOB, network="devnet", kind=GossipKind.PEER, payload={"url": "http://bob"}
    )
    assert envelope.sender == BOB_ADDRESS
    assert envelope.kind is GossipKind.PEER
    verify_envelope(envelope, network="devnet", public_key=BOB.public_key_hex)


def test_envelope_payload_is_canonical() -> None:
    body = envelope_payload("devnet", GossipKind.TRANSACTION, {"b": 1, "a": 2}, BOB_ADDRESS)
    assert body == {
        "network": "devnet",
        "kind": "transaction",
        "payload": {"b": 1, "a": 2},
        "sender": BOB_ADDRESS,
    }


def test_envelope_for_another_network_is_rejected() -> None:
    envelope = sign_envelope(BOB, network="devnet", kind=GossipKind.PEER, payload={"url": "x"})
    with pytest.raises(GossipError, match="network"):
        verify_envelope(envelope, network="testnet", public_key=BOB.public_key_hex)


def test_tampered_payload_breaks_the_signature() -> None:
    envelope = sign_envelope(BOB, network="devnet", kind=GossipKind.PEER, payload={"url": "x"})
    tampered = envelope.model_copy(update={"payload": {"url": "http://evil"}})
    with pytest.raises(GossipError, match="bad signature"):
        verify_envelope(tampered, network="devnet", public_key=BOB.public_key_hex)


def test_relabelled_kind_breaks_the_signature() -> None:
    envelope = sign_envelope(BOB, network="devnet", kind=GossipKind.PEER, payload={"url": "x"})
    relabelled = envelope.model_copy(update={"kind": GossipKind.BLOCK})
    with pytest.raises(GossipError, match="bad signature"):
        verify_envelope(relabelled, network="devnet", public_key=BOB.public_key_hex)


def test_non_hex_signature_and_key_are_rejected() -> None:
    envelope = sign_envelope(BOB, network="devnet", kind=GossipKind.PEER, payload={"url": "x"})
    broken = envelope.model_copy(update={"signature": "zz"})
    with pytest.raises(GossipError, match="not hex"):
        verify_envelope(broken, network="devnet", public_key=BOB.public_key_hex)
    with pytest.raises(GossipError, match="cannot verify"):
        verify_envelope(envelope, network="devnet", public_key="not-hex")


def test_block_and_proof_ids_are_recomputed(
    signed_match: Any, devnet_chain: Any, mine_block: Any
) -> None:
    proof = signed_match.proof
    document = {"proof": proof.model_dump(mode="json")}
    assert object_id_of(GossipKind.PROOF, document) == proof.proof_id
    miner = keypair_from_seed(bytes([3]) * 32)
    block = mine_block(devnet_chain, miner)
    payload = {"block": block.model_dump(mode="json")}
    assert object_id_of(GossipKind.BLOCK, payload) == block_hash(block.header)
    assert object_id_of(GossipKind.PEER, {"url": "http://a"}) == "http://a"


def test_transaction_object_id_is_the_txid(devnet_chain: Any) -> None:
    tx = WalletService(DEVNET, devnet_chain.state).build_transfer(
        faucet_keypair("devnet"), BOB_ADDRESS, 1_000
    )
    document = {"transaction": tx.model_dump(mode="json", by_alias=True)}
    assert object_id_of(GossipKind.TRANSACTION, document) == txid(tx)


def test_object_id_rejects_malformed_payloads() -> None:
    with pytest.raises(GossipError, match="cannot compute"):
        object_id_of(GossipKind.PROOF, {"proof": {"nope": True}})
    with pytest.raises(GossipError, match="needs a url"):
        object_id_of(GossipKind.PEER, {})
    with pytest.raises(GossipError, match="cannot compute"):
        object_id_of(GossipKind.TRANSACTION, {"transaction": {"nope": True}})


def test_peer_table_add_and_refresh() -> None:
    table = PeerTable()
    record = table.add("http://a", address=BOB_ADDRESS, height=3, now=10)
    assert len(table) == 1
    assert "http://a" in table
    assert table.get("http://a") is record
    assert record.height == 3 and record.last_seen == 10
    table.add("http://a", height=1, now=5)
    assert record.height == 3 and record.last_seen == 10
    table.remove("http://a")
    assert len(table) == 0
    assert "http://a" not in table
    assert table.get("http://a") is None


def test_peer_table_rejects_empty_url_and_overflows() -> None:
    with pytest.raises(GossipError, match="empty"):
        PeerTable().add("")
    table = PeerTable(max_peers=1)
    table.add("http://a")
    with pytest.raises(GossipError, match="full"):
        table.add("http://b")


def test_failures_ban_a_peer_until_the_ban_expires() -> None:
    table = PeerTable()
    table.add("http://a")
    for attempt in range(MAX_PEER_FAILURES - 1):
        table.record_failure("http://a", now=attempt)
    assert not table.is_banned("http://a", now=0)
    record = table.record_failure("http://a", now=10)
    assert record.banned_until == 10 + BAN_SECONDS
    assert table.is_banned("http://a", now=10 + BAN_SECONDS - 1)
    assert not table.is_banned("http://a", now=10 + BAN_SECONDS)
    assert [peer.url for peer in table.eligible(now=10)] == []
    assert [peer.url for peer in table.eligible(now=10 + BAN_SECONDS)] == ["http://a"]
    table.record_success("http://a", height=4, now=10 + BAN_SECONDS)
    assert record.failures == 0 and record.banned_until == 0 and record.height == 4


def test_gossip_cache_remembers_and_evicts() -> None:
    cache = GossipCache(max_entries=2)
    assert cache.check_and_add(GossipKind.PEER, "a", now=1)
    assert not cache.check_and_add(GossipKind.PEER, "a", now=2)
    assert cache.check_and_add(GossipKind.PEER, "b")
    assert cache.check_and_add(GossipKind.BLOCK, "a")
    assert len(cache) == 2
    assert cache.check_and_add(GossipKind.PROOF, "c")
    assert GossipKind.PEER not in {kind for kind, _ in cache.seen}


def test_fanout_excludes_the_sender_and_samples() -> None:
    peers = [PeerRecord(url=f"http://{index}") for index in range(6)]
    everyone = choose_fanout(peers, rng=random.Random(1), count=10)
    assert everyone == tuple(sorted(peer.url for peer in peers))
    sampled = choose_fanout(peers, rng=random.Random(1), count=2, exclude=["http://0"])
    assert len(sampled) == 2
    assert "http://0" not in sampled
    again = choose_fanout(peers, rng=random.Random(1), count=2, exclude=["http://0"])
    assert sampled == again


def test_plan_sync_covers_caught_up_and_clamped_ranges() -> None:
    assert plan_sync(5, 5) == (6, 5)
    assert plan_sync(5, 4) == (6, 5)
    assert plan_sync(0, 3) == (1, 3)
    assert plan_sync(0, 10, max_blocks=4) == (1, 4)


class _StubPeer:
    def __init__(self, blocks: dict[str, Any]) -> None:
        self.blocks = blocks

    def status(self) -> dict[str, object]:
        return {}

    def block(self, reference: str) -> dict[str, object]:
        return self.blocks[reference]


def test_fetch_blocks_accepts_wrapped_and_bare_documents(
    devnet_chain: Any, mine_block: Any
) -> None:
    miner = keypair_from_seed(bytes([3]) * 32)
    first = mine_block(devnet_chain, miner)
    second = mine_block(devnet_chain, miner)
    peer = _StubPeer(
        {"1": {"block": first.model_dump(mode="json")}, "2": second.model_dump(mode="json")}
    )
    blocks = fetch_blocks(peer, start_height=1, end_height=2)
    assert [block.header.height for block in blocks] == [1, 2]


def test_fetch_blocks_rejects_wrong_heights_and_malformed_documents(
    devnet_chain: Any, mine_block: Any
) -> None:
    miner = keypair_from_seed(bytes([3]) * 32)
    block = mine_block(devnet_chain, miner)
    wrong = _StubPeer({"2": {"block": block.model_dump(mode="json")}})
    with pytest.raises(GossipError, match="returned height 1"):
        fetch_blocks(wrong, start_height=2, end_height=2)
    malformed = _StubPeer({"1": {"block": {"nope": True}}})
    with pytest.raises(GossipError, match="malformed block"):
        fetch_blocks(malformed, start_height=1, end_height=1)


def test_envelopes_cannot_be_replayed_on_another_network() -> None:
    envelope = sign_envelope(
        BOB, network=TESTNET.network_id, kind=GossipKind.PEER, payload={"url": "http://b"}
    )
    with pytest.raises(GossipError, match="testnet"):
        verify_envelope(envelope, network=DEVNET.network_id, public_key=BOB.public_key_hex)
