from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
import chess
import httpx

from sccp.chain.storage import Storage
from sccp.chess.engines import MockEngine
from sccp.config import DEVNET
from sccp.crypto.address import encode_address
from sccp.crypto.keys import keypair_from_seed, sign_domain
from sccp.devnet import (
    GENESIS_EPOCH,
    DevnetMiner,
    _bootstrap,
    _mine_pending,
    _relay_for_epoch,
)
from sccp.miner.engine_miner import EngineMiner, MinerConfig, RelayClient, monotonic_ms
from sccp.miner.matchmaker import create_app as create_relay_app
from sccp.node.service import NodeService
from sccp.protocol.transcript import certificate_payload, proof_from_transcript, sign_result
from sccp.serialization import Domain
from sccp.transport import ASGITransport

path = Path("/tmp/spike_chain")
shutil.rmtree(path, ignore_errors=True)
path.mkdir()
service = NodeService.open(DEVNET, storage=Storage(path / "chain.sqlite"))
service.check_startup()
human_key = keypair_from_seed(bytes([7]) * 32)
opp_key = keypair_from_seed(bytes.fromhex("a1" * 32))
human_addr = encode_address(human_key.public_key_bytes, DEVNET)
opp_addr = encode_address(opp_key.public_key_bytes, DEVNET)
participants = [
    DevnetMiner(human_key, MockEngine(seed=7), hashlib.sha256(b"human").hexdigest()),
    DevnetMiner(opp_key, MockEngine(seed=11), hashlib.sha256(b"opponent").hexdigest()),
]
_bootstrap(service, participants)
print("after bootstrap", service.height)
pair = _relay_for_epoch(service, GENESIS_EPOCH, participants)
relay = pair.relay
relay.open_round(0)
views = relay.matches()
print("views", [(v.matchup.match_id, v.matchup.white, v.matchup.black, v.status) for v in views])
match = next(v for v in views if human_addr in (v.matchup.white, v.matchup.black))
sig = sign_domain(human_key, Domain.CERTIFICATE, certificate_payload(match.certificate)).hex()
human_relay = RelayClient(
    "http://relay", client=httpx.Client(transport=ASGITransport(create_relay_app(relay)), base_url="http://relay")
)
miner_relay = pair.relay_client
view = human_relay.join(
    match.matchup.match_id,
    seat=human_addr,
    public_key=human_key.public_key_hex,
    signature=sig,
)
config = MinerConfig(
    params=DEVNET,
    keypair=opp_key,
    relay_url="http://relay",
    max_games_per_epoch=DEVNET.rounds_per_epoch,
)
miner = EngineMiner(
    config, engine=MockEngine(seed=11), relay=miner_relay, node=pair.node_client, clock_ms=monotonic_ms
)
for _ in range(100):
    tick = miner.run_once()
    view = human_relay.match(match.matchup.match_id)
    if tick.joined or tick.refusals or tick.observed:
        print("tick", tick, "miner matches", miner.matches.keys())
    if view.status == "active":
        break
print("status", view.status, view.san_moves, "refusals", miner.refusals)
ourcolor = "white" if match.matchup.white == human_addr else "black"
for _ in range(200):
    view = human_relay.match(match.matchup.match_id)
    if view.status != "active":
        break
    board = chess.Board()
    for san in view.san_moves:
        board.push(board.parse_san(san))
    if (board.turn == chess.WHITE) == (ourcolor == "white"):
        if len(view.san_moves) >= 20:
            view = human_relay.resign(match.matchup.match_id, seat=human_addr)
            print("resigned", view.status, view.result)
            break
        move = next(iter(board.legal_moves))
        from sccp.protocol.clocks import sign_move
        from sccp.protocol.models import MoveMessage
        from sccp.miner.engine_miner import EMPTY_SIGNATURE

        msg = MoveMessage(
            network=DEVNET.network_id,
            match_id=match.matchup.match_id,
            seat=human_addr,
            ply=board.ply(),
            san=board.san(move),
            clock_ms=1_700_000,
            signature=EMPTY_SIGNATURE,
        )
        signed = sign_move(msg, human_key)
        human_relay.move(
            match.matchup.match_id,
            seat=human_addr,
            san=signed.san,
            network=signed.network,
            ply=signed.ply,
            clock_ms=signed.clock_ms,
            signature=signed.signature,
        )
    miner.run_once()
print("result", view.status, view.result)
for _ in range(5):
    view = human_relay.match(match.matchup.match_id)
    if (
        view.result
        and "white" in view.result.result_signatures
        and "black" in view.result.result_signatures
    ):
        break
    if view.result and ourcolor not in view.result.result_signatures:
        signed = sign_result(view.result, view.certificate, ourcolor, human_key)
        view = human_relay.result(
            match.matchup.match_id,
            seat=human_addr,
            signature=signed.result_signatures[ourcolor],
        )
        print("human signed")
    miner.run_once()
view = human_relay.match(match.matchup.match_id)
print("sigs", view.result.result_signatures.keys() if view.result else None)
transcript = human_relay.transcript(match.matchup.match_id)
print("transcript", transcript.result.result, transcript.result.termination, transcript.pgn[:100])
proof = proof_from_transcript(transcript, params=DEVNET)
print("proof winner", proof.winner, "human?", proof.winner == human_addr)
if proof.proof_id not in service.mempool.proofs:
    pair.node_client.announce_proof(proof)
print("mempool proofs", len(service.mempool.proofs))
keypairs = {human_addr: human_key, opp_addr: opp_key}
mined: list[str] = []
print("mine", _mine_pending(service, keypairs, mined), mined, "height", service.height)
print("block", service.block(str(service.height)).block.proofs[0].proof_id)
service.close()
