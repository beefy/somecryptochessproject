"""A miner as a challenge panel member: re-run, vote, retry.

The panel is derived by the node from the beacon and the bonded set; a member
re-runs the pinned quality profile over the challenged proof and commits its
verdict in a signed ``panel_vote`` transaction. These tests drive that loop with
a uniform analysis engine, so the outcome is decided by the summary under review
rather than by which engine happens to be installed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import chess

from sccp.chess.engines import ScriptedEngine
from sccp.chess.pgn import validate_pgn
from sccp.chess.quality import QUALITY_V1, analyse_game
from sccp.config import DEVNET
from sccp.errors import RelayError
from sccp.miner.engine_miner import EngineMiner, MinerConfig, NodeClient
from sccp.protocol.models import QualitySummary, Transaction, TxType

CHALLENGE_ID = "cd" * 32
PARAMS = DEVNET.model_copy(update={"min_plies": 1})
"""Devnet with short games allowed, so the fixture proof needs no fillers."""


class _UniformEngine:
    """A deterministic zero-centipawn engine: every move is best."""

    name = "uniform-analysis"

    @property
    def fingerprint(self) -> str:
        return "uniform-analysis-v1"

    def analyse(self, board: Any, limit: Any, *, multipv: int = 1) -> list[Any]:
        legal = list(board.legal_moves)
        if not legal:
            return [
                {
                    "score": chess.engine.PovScore(chess.engine.Mate(0), board.turn),
                    "pv": [],
                }
            ]
        return [
            {
                "score": chess.engine.PovScore(chess.engine.Cp(0), board.turn),
                "pv": [legal[0]],
            }
        ]


class _NoMatches:
    """A relay with nothing to play, so a tick is only about adjudication."""

    def matches(self) -> list[Any]:
        return []


@dataclass
class _StubNode:
    """The node endpoints a voting miner uses, scripted per test."""

    game_hash: str
    pgn: str
    summary: dict[str, Any] | None
    adjudicators: tuple[str, ...]
    nonce: int = 0
    submitted: list[Transaction] = field(default_factory=list)
    fail_submit: str | None = None

    def challenges(self) -> list[dict[str, Any]]:
        return [
            {
                "challenge_id": CHALLENGE_ID,
                "network": DEVNET.network_id,
                "proof_id": "ab" * 32,
                "game_hash": self.game_hash,
                "summary": self.summary,
                "adjudicators": list(self.adjudicators),
                "votes": {},
                "status": "open",
            }
        ]

    def account(self, address: str) -> dict[str, Any]:
        return {"address": address, "nonce": self.nonce}

    def game_pgn(self, game_hash: str) -> str:
        assert game_hash == self.game_hash
        return self.pgn

    def submit_transaction(self, tx: Transaction) -> dict[str, Any]:
        if self.fail_submit is not None:
            raise RelayError(self.fail_submit)
        self.submitted.append(tx)
        return {"network": DEVNET.network_id, "txid": "00" * 32}


def _honest_summary(proof: Any) -> QualitySummary:
    parsed = validate_pgn(proof.pgn, params=PARAMS, require_win=True)
    report = analyse_game(parsed, _UniformEngine(), profile=QUALITY_V1)
    return report.summary()


def _miner(accounts: Any, node: _StubNode, *, summary_policy: str = "auto") -> EngineMiner:
    config = MinerConfig(
        params=PARAMS,
        keypair=accounts["alice"].keypair,
        summary_policy=summary_policy,  # type: ignore[arg-type]
    )
    return EngineMiner(
        config,
        engine=ScriptedEngine([]),
        relay=_NoMatches(),  # type: ignore[arg-type]
        node=node,  # type: ignore[arg-type]
        quality_engine=_UniformEngine(),
    )


def _node_for(signed_match: Any, accounts: Any, summary: QualitySummary | None) -> _StubNode:
    return _StubNode(
        game_hash=signed_match.proof.game_hash,
        pgn=signed_match.proof.pgn,
        summary=None if summary is None else summary.model_dump(mode="json"),
        adjudicators=(accounts["alice"].address,),
    )


def test_an_honest_summary_is_voted_down(signed_match, protocol_accounts) -> None:
    honest = _honest_summary(signed_match.proof)
    node = _node_for(signed_match, protocol_accounts, honest)
    tick = _miner(protocol_accounts, node).run_once()

    assert tick.votes == (CHALLENGE_ID,)
    (tx,) = node.submitted
    assert tx.type is TxType.PANEL_VOTE
    assert tx.payload["challenge_id"] == CHALLENGE_ID
    assert tx.payload["upheld"] is False


def test_a_false_summary_is_upheld(signed_match, protocol_accounts) -> None:
    lying = _honest_summary(signed_match.proof).model_copy(update={"white_blunders": 9})
    node = _node_for(signed_match, protocol_accounts, lying)
    tick = _miner(protocol_accounts, node).run_once()

    assert tick.votes == (CHALLENGE_ID,)
    (tx,) = node.submitted
    assert tx.payload["upheld"] is True


def test_a_proof_without_a_summary_is_a_disagreement(signed_match, protocol_accounts) -> None:
    node = _node_for(signed_match, protocol_accounts, None)
    tick = _miner(protocol_accounts, node).run_once()

    assert tick.votes == (CHALLENGE_ID,)
    assert node.submitted[0].payload["upheld"] is True


def test_a_challenge_this_miner_does_not_judge_is_left_alone(
    signed_match, protocol_accounts
) -> None:
    node = _node_for(signed_match, protocol_accounts, _honest_summary(signed_match.proof))
    node.adjudicators = ("sccpt1somebodyelse",)
    tick = _miner(protocol_accounts, node).run_once()

    assert tick.votes == ()
    assert node.submitted == []


def test_the_ballot_is_signed_by_the_miner_with_the_chain_nonce(
    signed_match, protocol_accounts
) -> None:
    node = _node_for(signed_match, protocol_accounts, _honest_summary(signed_match.proof))
    node.nonce = 7
    _miner(protocol_accounts, node).run_once()

    (tx,) = node.submitted
    assert tx.sender == protocol_accounts["alice"].address
    assert tx.nonce == 7
    assert tx.fee == PARAMS.min_fee
    assert tx.signature is not None
    assert tx.payload["public_key"] == protocol_accounts["alice"].keypair.public_key_hex
    assert json.loads(tx.model_dump_json(by_alias=True))["type"] == "panel_vote"


def test_a_refused_vote_is_retried_on_the_next_poll(signed_match, protocol_accounts) -> None:
    node = _node_for(signed_match, protocol_accounts, _honest_summary(signed_match.proof))
    node.fail_submit = "sender already has a pending transaction with nonce 0"
    miner = _miner(protocol_accounts, node)

    first = miner.run_once()
    assert first.votes == ()
    assert any("vote failed" in reason for reason in first.refusals)
    assert CHALLENGE_ID not in miner.voted

    node.fail_submit = None
    second = miner.run_once()
    assert second.votes == (CHALLENGE_ID,)
    assert len(node.submitted) == 1


def test_a_voted_challenge_is_not_voted_again(signed_match, protocol_accounts) -> None:
    node = _node_for(signed_match, protocol_accounts, _honest_summary(signed_match.proof))
    miner = _miner(protocol_accounts, node)

    assert miner.run_once().votes == (CHALLENGE_ID,)
    assert miner.run_once().votes == ()
    assert len(node.submitted) == 1


def test_a_miner_without_a_node_does_not_adjudicate(signed_match, protocol_accounts) -> None:
    miner = EngineMiner(
        MinerConfig(params=PARAMS, keypair=protocol_accounts["alice"].keypair),
        engine=ScriptedEngine([]),
        relay=_NoMatches(),  # type: ignore[arg-type]
        quality_engine=_UniformEngine(),
    )
    assert miner.run_once().votes == ()


def test_the_summary_policy_controls_what_a_winner_attaches(
    signed_match, protocol_accounts
) -> None:
    proof = signed_match.proof
    node = _node_for(signed_match, protocol_accounts, None)

    automatic = _miner(protocol_accounts, node)
    assert automatic._quality_summary(proof) is None  # devnet runs with quality off

    honest = _miner(protocol_accounts, node, summary_policy="honest")
    assert honest._quality_summary(proof) == _honest_summary(proof)

    forged = _miner(protocol_accounts, node, summary_policy="forged")
    claim = forged._quality_summary(proof)
    assert claim is not None
    assert (claim.verdict, claim.white_blunders, claim.black_blunders) == ("pass", 0, 0)
    assert (claim.worst_cpl, claim.trace_hash) == (0, None)


def test_the_node_reports_the_panel_and_the_claim_a_miner_judges() -> None:
    """The miner's HTTP shapes match the node's real ``ChallengeView``."""
    from sccp.chain.state import ChallengeRecord
    from sccp.crypto.address import encode_address
    from sccp.crypto.pq import hybrid_keypair_from_seed
    from sccp.node.app import create_app
    from sccp.node.service import NodeService
    from sccp.transport import client_for

    judge = hybrid_keypair_from_seed(b"\x07" * 32)
    judge_address = encode_address(judge.public_key_bytes, DEVNET)
    miner = hybrid_keypair_from_seed(b"\x08" * 32)
    challenger = hybrid_keypair_from_seed(b"\x09" * 32)
    challenger_address = encode_address(challenger.public_key_bytes, DEVNET)

    service = NodeService.open(DEVNET)
    try:
        state = service.chain.state
        miner_address = encode_address(miner.public_key_bytes, DEVNET)
        state.ensure(judge_address).bonded = 10
        state.ensure(miner_address).bonded = 10
        record = ChallengeRecord(
            challenge_id="cd" * 32,
            proof_id="ab" * 32,
            game_hash="ee" * 32,
            challenger=challenger_address,
            miner=miner_address,
            reason="summary does not match",
            evidence="{}",
            bond=5,
            opened_height=1,
            deadline_height=11,
            votes={judge_address: True},
        )
        state.challenges[record.challenge_id] = record

        with client_for(create_app(service)) as client:
            node = NodeClient("http://node", client=client)
            listed = node.challenges()
            view = node.challenge(record.challenge_id)

        (entry,) = listed
        assert entry["challenge_id"] == record.challenge_id
        assert tuple(entry["adjudicators"]) == (judge_address,)
        assert view["votes"] == {judge_address: True}
        assert view["summary"] is None  # no block body in this chain to read it from
        assert view["status"] == "open"
    finally:
        service.close()


def test_an_honest_claim_survives_the_panels_re_run(signed_match, protocol_accounts) -> None:
    """The forgery is only caught when it disagrees; an honest claim is not."""
    proof = signed_match.proof
    honest_miner = _miner(
        protocol_accounts,
        _node_for(signed_match, protocol_accounts, None),
        summary_policy="honest",
    )
    claim = honest_miner._quality_summary(proof)
    assert claim is not None

    # A different member reviews that claim with its own engine.
    node = _node_for(signed_match, protocol_accounts, claim)
    tick = _miner(protocol_accounts, node).run_once()
    assert tick.votes == (CHALLENGE_ID,)
    assert node.submitted[-1].payload["upheld"] is False
