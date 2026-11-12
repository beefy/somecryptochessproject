"""Adversarial quality, challenge and panel cases (plan M5 acceptance criteria).

These run with the ``adversarial`` marker, which the default suite deselects:

    make adversarial

Every test uses mock engines, so the suite needs no Stockfish binary and stays
deterministic. The attacks are the ones the plan names: a fabricated summary, a
challenger that tries to judge its own case, a node that picks a different panel,
stuffed or silent ballots, and a poisoned engine that would file challenges on an
honest miner's behalf.
"""

from __future__ import annotations

import json
import random
from dataclasses import replace

import chess
import pytest

from sccp.chain.state import ProofRecord, State
from sccp.chess.pgn import validate_pgn
from sccp.chess.quality import QUALITY_V1, QualityProfile, analyse_game
from sccp.config import NetworkParams, QualityMode
from sccp.errors import ChallengeError, QualityError
from sccp.node.quality_sampler import QualitySampler
from sccp.protocol import challenges

pytestmark = pytest.mark.adversarial

PROFILE = QualityProfile(
    policy=QUALITY_V1.policy,
    engine=QUALITY_V1.engine,
    depth=1,
    max_cpl=0,
    decided_cp=0,
    max_blunders_per_side=1_000,
    resign_cp=300,
    min_plies=1,
)
BOND = 5_000
EXTRA_BOND = 2 * BOND


class _UniformEngine:
    """A deterministic zero-centipawn engine, enough for the policy plumbing."""

    name = "uniform-analysis"

    @property
    def fingerprint(self) -> str:
        return "uniform-analysis-v1"

    def analyse(self, board, limit, *, multipv=1):
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


class _WeakLoserEngine(_UniformEngine):
    """Scores only the moves the losing side actually played as lost."""

    name = "weak-loser"
    fingerprint = "weak-loser-v1"

    def __init__(self, played: set[tuple[str, str]]) -> None:
        self._played = played

    def analyse(self, board, limit, *, multipv=1):
        return [
            {
                "score": chess.engine.PovScore(
                    chess.engine.Cp(-2_000 if (board.fen(), move.uci()) in self._played else 0),
                    board.turn,
                ),
                "pv": [move],
            }
            for move in board.legal_moves
        ]


def _loser_positions(parsed) -> set[tuple[str, str]]:
    """``(fen, uci)`` for the losing side's mainline moves."""
    loser = chess.BLACK if parsed.result.value == "1-0" else chess.WHITE
    board = chess.Board(parsed.start_fen)
    played: set[tuple[str, str]] = set()
    for san in parsed.san_moves:
        move = board.parse_san(san)
        if board.turn == loser:
            played.add((board.fen(), move.uci()))
        board.push(move)
    return played


class _PoisonedEngine:
    """A local engine that fails, as a compromised validator's would."""

    name = "poisoned"
    fingerprint = "poisoned"

    def analyse(self, board, limit, *, multipv=1):
        raise QualityError("poisoned engine output")


def _sampled(params: NetworkParams) -> NetworkParams:
    return params.model_copy(
        update={"quality_mode": QualityMode.SAMPLED, "quality_sample_rate": 1_000}
    )


def _challenge_ledger(
    params: NetworkParams, accounts, *, challenger: str = "bob"
) -> tuple[State, str]:
    """A ledger with one accepted proof and an open challenge against it."""
    state = State(params=params, height=5, timestamps=[1_700_000_000])
    for account in accounts.values():
        entry = state.ensure(account.address)
        entry.public_key = account.keypair.public_key_hex
        entry.balance = 1_000_000
    proof_id, game_hash = "ab" * 32, "cd" * 32
    state.proofs[game_hash] = ProofRecord(
        proof_id=proof_id,
        game_hash=game_hash,
        height=2,
        winner=accounts["alice"].address,
        miner=accounts["alice"].address,
    )
    state.proof_ids[proof_id] = game_hash
    record = challenges.open_challenge(
        state,
        challenger=accounts[challenger].address,
        proof_id=proof_id,
        reason="fabricated trace",
        evidence="trace diverges at ply 6",
        bond=BOND,
        height=4,
    )
    return state, record.challenge_id


def _bond_extra(state: State, *addresses: str) -> tuple[str, ...]:
    for address in addresses:
        state.ensure(address).bonded = EXTRA_BOND
    return addresses


def test_a_fabricated_summary_becomes_a_bonded_challenge(
    signed_match, protocol_params: NetworkParams
) -> None:
    """A summary that re-analysis cannot reproduce is an economic event, not a block error."""
    params = _sampled(protocol_params)
    parsed = validate_pgn(signed_match.proof.pgn, params=params, require_win=True)
    report = analyse_game(parsed, _UniformEngine(), profile=PROFILE)
    summary = report.summary().model_copy(update={"black_blunders": report.black_blunders + 3})
    proof = signed_match.proof.model_copy(update={"quality_summary": summary})
    sampler = QualitySampler(params=params, profile=PROFILE)
    sampler.enqueue("11" * 32, proof)

    (result,) = sampler.run(_UniformEngine())
    (request,) = sampler.challenge_requests()

    assert result.needs_challenge is True
    assert request.proof_id == proof.proof_id
    document = json.loads(request.evidence)
    assert document["reported_trace_hash"] == summary.trace_hash
    assert document["observed_trace_hash"] == report.trace_hash
    assert "black blunders" in request.reason


def test_a_poisoned_engine_cannot_grief_an_honest_miner(
    signed_match, protocol_params: NetworkParams
) -> None:
    """A validator whose engine fails has no evidence, so it stays silent."""
    params = _sampled(protocol_params)
    sampler = QualitySampler(params=params, profile=PROFILE, max_attempts=1)
    sampler.enqueue("11" * 32, signed_match.proof)

    (result,) = sampler.run(_PoisonedEngine())

    assert result.error is not None
    assert result.challenge() is None
    assert sampler.challenge_requests() == ()


def test_a_challenger_cannot_pack_its_own_panel(
    protocol_params: NetworkParams, protocol_accounts
) -> None:
    """The two parties are excluded, and a shortage is an error, not a fallback."""
    state, challenge_id = _challenge_ledger(protocol_params, protocol_accounts)
    _bond_extra(state, protocol_accounts["carol"].address)

    with pytest.raises(ChallengeError, match="eligible adjudicator"):
        challenges.challenge_panel(state, challenge_id)

    _bond_extra(state, "ee" * 20, "fd" * 20)
    panel = challenges.challenge_panel(state, challenge_id)
    assert protocol_accounts["bob"].address not in panel
    assert protocol_accounts["alice"].address not in panel


def test_independent_nodes_derive_the_same_panel(
    protocol_params: NetworkParams, protocol_accounts
) -> None:
    """Panel choice depends on the beacon and the bonded set, not on iteration order."""
    state, challenge_id = _challenge_ledger(protocol_params, protocol_accounts)
    extra = _bond_extra(state, protocol_accounts["carol"].address, "ee" * 20, "fd" * 20)
    candidates = [account.address for account in protocol_accounts.values()] + list(extra)
    shuffled = list(candidates)
    random.Random(7).shuffle(shuffled)

    expected = challenges.select_panel(
        "00" * 32,
        "ab" * 32,
        candidates,
        exclude=(protocol_accounts["bob"].address, protocol_accounts["alice"].address),
    )
    observed = challenges.select_panel(
        "00" * 32,
        "ab" * 32,
        shuffled,
        exclude=(protocol_accounts["alice"].address, protocol_accounts["bob"].address),
    )

    assert expected == observed == challenges.challenge_panel(state, challenge_id)


def test_stuffed_or_silent_ballots_cannot_resolve(
    protocol_params: NetworkParams, protocol_accounts
) -> None:
    """Neither an extra voter nor a missing one may decide a challenge."""
    state, challenge_id = _challenge_ledger(protocol_params, protocol_accounts)
    _bond_extra(state, protocol_accounts["carol"].address, "ee" * 20, "fd" * 20)
    panel = challenges.challenge_panel(state, challenge_id)

    stuffed = {**dict.fromkeys(panel, True), "cc" * 20: True}
    with pytest.raises(ChallengeError, match="unknown"):
        challenges.adjudicate_with_panel(state, challenge_id, stuffed, height=5)

    silent = dict.fromkeys(panel[:-1], True)
    with pytest.raises(ChallengeError, match="missing"):
        challenges.adjudicate_with_panel(state, challenge_id, silent, height=5)

    assert state.challenges[challenge_id].status == "open"


def test_a_weak_loser_game_fails_quality_and_the_challenge_holds(
    signed_match, protocol_params: NetworkParams, protocol_accounts
) -> None:
    """The plan's acceptance case: a legal but weak game is provably bad."""
    parsed = signed_match.parsed
    profile = replace(PROFILE, max_cpl=100, decided_cp=10_000, max_blunders_per_side=0)
    report = analyse_game(parsed, _WeakLoserEngine(_loser_positions(parsed)), profile=profile)

    assert report.verdict == "fail"
    assert report.white_blunders == 0
    assert report.black_blunders == 3
    assert report.worst_cpl > profile.max_cpl

    state, challenge_id = _challenge_ledger(protocol_params, protocol_accounts)
    _bond_extra(state, protocol_accounts["carol"].address, "ee" * 20, "fd" * 20)
    panel = challenges.challenge_panel(state, challenge_id)
    record = challenges.adjudicate_with_panel(
        state, challenge_id, dict.fromkeys(panel, True), height=5
    )

    assert record.status == "upheld"
    assert record.panel == panel
    assert state.ensure(protocol_accounts["alice"].address).bonded == 0
    assert state.ensure(protocol_accounts["bob"].address).balance == 1_000_000


def test_a_colluding_pair_cannot_be_its_own_jury(
    protocol_params: NetworkParams, protocol_accounts
) -> None:
    """Even with a majority of bonds, the parties never adjudicate their own case."""
    state, challenge_id = _challenge_ledger(protocol_params, protocol_accounts)
    miner = protocol_accounts["alice"].address
    challenger = protocol_accounts["bob"].address
    state.ensure(miner).bonded = 10 * BOND
    state.ensure(challenger).bonded = 10 * BOND
    _bond_extra(state, protocol_accounts["carol"].address, "ee" * 20, "fd" * 20)

    panel = challenges.challenge_panel(state, challenge_id)

    assert miner not in panel and challenger not in panel
    assert protocol_accounts["carol"].address in panel
