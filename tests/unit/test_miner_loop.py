"""The unattended engine miner, driven by a stub relay."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sccp.chess.engines import ScriptedEngine
from sccp.chess.openings import book_for
from sccp.config import DEVNET
from sccp.errors import EpochPipelineError, RelayError
from sccp.miner.engine_miner import EngineMiner, MinerConfig, NodeClient
from sccp.miner.epoch_pipeline import PipelineAction
from sccp.miner.matchmaker import MatchView
from sccp.protocol.models import GameResult, Matchup, WinProof
from sccp.protocol.transcript import create_certificate

MATCH_ID = "ab" * 32
OPENING = tuple(book_for(DEVNET).select(bytes(32), MATCH_ID).moves)
SECRET = "22" * 32
"""A valid 32-byte hex availability nonce for the pipeline wiring tests."""


@dataclass
class _StubRelay:
    """A relay that records what the miner did and can be scripted to answer."""

    view: MatchView
    transcript_doc: Any = None
    calls: list[tuple[str, ...]] = field(default_factory=list)
    fail: str | None = None
    closed: bool = False

    def _guard(self) -> None:
        if self.fail is not None:
            raise RelayError(self.fail)

    def matches(self) -> list[MatchView]:
        self._guard()
        return [self.view]

    def match(self, match_id: str) -> MatchView:
        self._guard()
        assert match_id == self.view.matchup.match_id
        return self.view

    def join(self, match_id: str, *, seat: str, public_key: str, signature: str) -> MatchView:
        self._guard()
        self.calls.append(("join", seat))
        colour = "white" if seat == self.view.matchup.white else "black"
        signatures = dict(self.view.certificate.start_signatures)
        signatures[colour] = signature
        certificate = self.view.certificate.model_copy(update={"start_signatures": signatures})
        both = set(signatures) == {"white", "black"}
        status = "active" if both else "pending"
        self.view = self.view.model_copy(update={"status": status, "certificate": certificate})
        return self.view

    def move(
        self,
        match_id: str,
        *,
        seat: str,
        san: str,
        network: str | None = None,
        ply: int | None = None,
        clock_ms: int | None = None,
        signature: str | None = None,
    ) -> MatchView:
        self._guard()
        self.calls.append(("move", seat, san))
        if signature is not None:
            self.calls.append(("signed", str(ply), str(network)))
        self.view = self.view.model_copy(update={"san_moves": (*self.view.san_moves, san)})
        return self.view

    def receipts(self, match_id: str) -> Any:
        """No timed sessions in the stub: the miner falls back to local receipts."""
        self._guard()
        return None

    def result(self, match_id: str, *, seat: str, signature: str) -> MatchView:
        self._guard()
        self.calls.append(("result", seat))
        return self.view

    def resign(self, match_id: str, *, seat: str) -> MatchView:
        """Record a resignation and finish the session from the mover's side."""
        self._guard()
        self.calls.append(("resign", seat))
        white_moved = len(self.view.san_moves) % 2 == 0
        loser = self.view.matchup.white if white_moved else self.view.matchup.black
        winner = "0-1" if loser == self.view.matchup.white else "1-0"
        result = GameResult(
            network=self.view.matchup.network,
            match_id=self.view.matchup.match_id,
            move_commitment="00" * 32,
            game_hash="ef" * 32,
            result=winner,
            termination="resignation",
            plies=len(self.view.san_moves),
            result_signatures={},
        )
        self.view = self.view.model_copy(update={"status": "finished", "result": result})
        return self.view

    def close(self) -> None:
        self.closed = True

    def transcript(self, match_id: str) -> Any:
        self._guard()
        if self.transcript_doc is None:
            raise RelayError("no transcript")
        return self.transcript_doc


def _matchup(white: str, black: str) -> Matchup:
    return Matchup(
        network=DEVNET.network_id, match_id=MATCH_ID, epoch=3, round=0, white=white, black=black
    )


def _view(
    white: str,
    black: str,
    *,
    status: str = "pending",
    moves: tuple[str, ...] = OPENING,
    result: GameResult | None = None,
) -> MatchView:
    matchup = _matchup(white, black)
    certificate = create_certificate(
        matchup,
        _line(matchup),
        engine_white="mock-v1:aaaaaaaaaaaaaaaa",
        engine_black="mock-v1:bbbbbbbbbbbbbbbb",
    )
    return MatchView(
        matchup=matchup,
        status=status,
        san_moves=moves,
        certificate=certificate,
        result=result,
    )


def _line(matchup: Matchup) -> Any:
    from sccp.chess.openings import book_for

    book = book_for(DEVNET)
    return book.select(bytes(32), matchup.match_id)


def _miner(
    accounts: Any,
    relay: _StubRelay,
    script: tuple[str, ...] = ("Nf3",),
    clock: Callable[[], int] = lambda: 0,
    *,
    resign_after_plies: int | None = None,
    node: Any = None,
) -> EngineMiner:
    alice = accounts["alice"]
    config = MinerConfig(
        params=DEVNET, keypair=alice.keypair, resign_after_plies=resign_after_plies
    )
    return EngineMiner(
        config,
        engine=ScriptedEngine(list(script)),
        relay=relay,  # type: ignore[arg-type]
        node=node,
        clock_ms=clock,
    )


def test_config_exposes_network_address_and_limit(protocol_accounts: Any) -> None:
    alice = protocol_accounts["alice"]
    config = MinerConfig.for_network("devnet", alice.keypair, nodes_per_move=1234)
    assert config.network == "devnet"
    assert config.address == alice.address
    assert config.limit.nodes == 1234
    deeper = MinerConfig.for_network("devnet", alice.keypair, depth=3)
    assert deeper.limit.depth == 3


def test_relay_outage_is_recorded_as_a_refusal(protocol_accounts: Any) -> None:
    alice = protocol_accounts["alice"]
    relay = _StubRelay(_view(alice.address, "sccpt1peerpeerpeer"), fail="connection refused")
    miner = _miner(protocol_accounts, relay)
    tick = miner.run_once()
    assert tick.observed == 0
    assert tick.refusals and "relay unavailable" in tick.refusals[0]


def test_matches_we_are_not_seated_in_are_ignored(protocol_accounts: Any) -> None:
    relay = _StubRelay(_view("sccpt1otherotherother", "sccpt1otherotherothe2"))
    miner = _miner(protocol_accounts, relay)
    tick = miner.run_once()
    assert tick.observed == 0
    assert relay.calls == []


def test_pending_match_is_joined_and_then_played(protocol_accounts: Any) -> None:
    alice, bob = protocol_accounts["alice"], protocol_accounts["bob"]
    relay = _StubRelay(_view(alice.address, bob.address))
    miner = _miner(protocol_accounts, relay)
    first = miner.run_once()
    assert first.joined == (MATCH_ID,)
    assert relay.calls == [("join", alice.address)]

    # The opponent joins, so the relay activates the session and it is our move.
    relay.view = relay.view.model_copy(update={"status": "active"})
    second = miner.run_once()
    assert second.moves and relay.calls[1] == ("move", alice.address, "Nf3")
    assert miner.matches[MATCH_ID].san_moves == [*OPENING, "Nf3"]


def test_a_pending_match_is_not_joined_twice(protocol_accounts: Any) -> None:
    """A seat that is waiting for its opponent must not re-join every poll."""
    alice, bob = protocol_accounts["alice"], protocol_accounts["bob"]
    relay = _StubRelay(_view(alice.address, bob.address))
    miner = _miner(protocol_accounts, relay)
    first = miner.run_once()
    second = miner.run_once()
    assert first.joined == (MATCH_ID,)
    assert second.joined == ()
    assert second.refusals == ()
    assert [call for call in relay.calls if call[0] == "join"] == [("join", alice.address)]
    assert set(relay.view.certificate.start_signatures) == {"white"}


def test_the_miner_waits_when_it_is_not_to_move(protocol_accounts: Any) -> None:
    alice, bob = protocol_accounts["alice"], protocol_accounts["bob"]
    relay = _StubRelay(_view(alice.address, bob.address, status="finished"))
    relay.view = relay.view.model_copy(update={"status": "active", "san_moves": (*OPENING, "Nf3")})
    miner = _miner(protocol_accounts, relay)
    miner.run_once()
    before = len(relay.calls)
    second = miner.run_once()
    assert second.moves == ()
    assert len(relay.calls) == before


def test_a_stalled_game_is_resigned_on_our_turn(protocol_accounts: Any) -> None:
    """A drawn-out game must end decisively, or it never yields a proof."""
    alice, bob = protocol_accounts["alice"], protocol_accounts["bob"]
    relay = _StubRelay(_view(alice.address, bob.address, status="active"))
    miner = _miner(protocol_accounts, relay, resign_after_plies=len(OPENING))
    tick = miner.run_once()
    assert tick.resignations == (MATCH_ID,)
    assert tick.moves == ()
    assert ("resign", alice.address) in relay.calls
    assert relay.view.status == "finished"
    assert relay.view.result is not None
    assert relay.view.result.termination == "resignation"
    assert relay.view.result.result == "0-1"  # White (alice) resigned


def test_a_game_below_the_ply_limit_is_played_on(protocol_accounts: Any) -> None:
    alice, bob = protocol_accounts["alice"], protocol_accounts["bob"]
    relay = _StubRelay(_view(alice.address, bob.address, status="active"))
    miner = _miner(protocol_accounts, relay, resign_after_plies=len(OPENING) + 2)
    tick = miner.run_once()
    assert tick.resignations == ()
    assert tick.moves == (f"{MATCH_ID}:Nf3",)
    assert not [call for call in relay.calls if call[0] == "resign"]


def test_a_miner_never_resigns_when_it_is_not_to_move(protocol_accounts: Any) -> None:
    alice, bob = protocol_accounts["alice"], protocol_accounts["bob"]
    relay = _StubRelay(_view(alice.address, bob.address, status="active"))
    miner = _miner(protocol_accounts, relay, resign_after_plies=len(OPENING))
    # A relay report that adds a ply leaves Black to move, so White must not resign.
    relay.view = relay.view.model_copy(update={"san_moves": (*OPENING, "a6")})
    tick = miner.run_once()
    assert tick.resignations == ()
    assert not [call for call in relay.calls if call[0] == "resign"]


def test_resignation_is_off_by_default(protocol_accounts: Any) -> None:
    alice, bob = protocol_accounts["alice"], protocol_accounts["bob"]
    relay = _StubRelay(_view(alice.address, bob.address, status="active"))
    miner = _miner(protocol_accounts, relay)
    assert miner.config.resign_after_plies is None
    tick = miner.run_once()
    assert tick.resignations == ()
    assert tick.moves == (f"{MATCH_ID}:Nf3",)


def test_unreproducible_results_are_refused(protocol_accounts: Any) -> None:
    alice, bob = protocol_accounts["alice"], protocol_accounts["bob"]
    result = GameResult(
        network=DEVNET.network_id,
        match_id=MATCH_ID,
        move_commitment="00" * 32,
        game_hash="cd" * 32,
        result="0-1",
        termination="checkmate",
        plies=99,
        result_signatures={},
    )
    relay = _StubRelay(_view(alice.address, bob.address, status="finished", result=result))
    miner = _miner(protocol_accounts, relay, script=())
    tick = miner.run_once()
    assert tick.proofs == ()
    assert any("unreproducible" in reason for reason in tick.refusals)


def test_timeout_claims_need_local_state_and_a_flagged_clock(protocol_accounts: Any) -> None:
    alice, bob = protocol_accounts["alice"], protocol_accounts["bob"]
    relay = _StubRelay(_view(alice.address, bob.address))
    miner = _miner(protocol_accounts, relay)
    assert miner.build_timeout_claim(relay.view) is None
    assert any("no local state" in reason for reason in miner.refusals)
    miner.run_once()
    assert miner.build_timeout_claim(relay.view, witnesses=()) is None


def test_plan_builders_sign_real_transactions(live_chain: Any) -> None:
    alice = live_chain.accounts["alice"]
    params = live_chain.chain.params
    miner = EngineMiner(
        MinerConfig(params=params, keypair=alice.keypair),
        engine=ScriptedEngine([]),
        relay=_StubRelay(_view(alice.address, live_chain.accounts["bob"].address)),
        clock_ms=lambda: 0,
    )
    state = live_chain.chain.state
    secret = live_chain.nonce_secrets["alice"]
    availability = miner.plan_availability(state, nonce_secret=secret)
    reveal = miner.plan_reveal(state, nonce_secret=secret)
    register = miner.plan_register(state, bond=params.min_bond, endpoint="tcp://alice.test")
    assert availability.type == "availability"
    assert reveal.type == "reveal"
    assert register.type == "register"
    assert register.payload["engine"] == miner.engine.fingerprint


def test_receipts_are_kept_per_match(protocol_accounts: Any) -> None:
    alice = protocol_accounts["alice"]
    miner = _miner(protocol_accounts, _StubRelay(_view(alice.address, "sccpt1peerpeerpeer")))
    assert miner.receipts_for("unknown") == ()
    assert miner.books == {}


def test_timeout_claims_need_witness_receipts(protocol_accounts: Any) -> None:
    alice, bob = protocol_accounts["alice"], protocol_accounts["bob"]
    now = {"ms": 0}
    relay = _StubRelay(_view(alice.address, bob.address))
    miner = _miner(protocol_accounts, relay, clock=lambda: now["ms"])
    miner.run_once()  # join
    relay.view = relay.view.model_copy(update={"status": "active"})
    miner.run_once()  # one signed move, so a claim has evidence to attach
    now["ms"] = 10**9  # both clocks are long gone
    assert miner.build_timeout_claim(relay.view) is None
    assert miner.claims == {}
    assert any("witness receipts" in reason for reason in miner.refusals)


def _proof(proof_id: str = "ab" * 32, epoch: int = 3) -> Any:
    """A proof shell: the announcement tests only need the id."""
    return WinProof.model_construct(network=DEVNET.network_id, proof_id=proof_id, epoch=epoch)


@dataclass
class _StubNode:
    """A node whose proof admission the test scripts."""

    error: str | None = None
    attempts: list[str] = field(default_factory=list)
    accepted: list[str] = field(default_factory=list)

    def announce_proof(self, proof: Any) -> dict[str, object]:
        self.attempts.append(proof.proof_id)
        if self.error is not None:
            raise RelayError(self.error)
        self.accepted.append(proof.proof_id)
        return {"network": DEVNET.network_id, "proof_id": proof.proof_id}


def test_a_proof_announced_before_its_epoch_ends_is_retried(protocol_accounts: Any) -> None:
    alice, bob = protocol_accounts["alice"], protocol_accounts["bob"]
    relay = _StubRelay(_view(alice.address, bob.address))
    node = _StubNode(
        error=(
            "proof references epoch 3, which ends at height 19; settlement starts at height "
            "20 and the next block is 19"
        )
    )
    miner = _miner(protocol_accounts, relay, node=node)
    proof = _proof()
    assert not miner._announce_proof(proof)
    assert list(miner.pending_proofs) == [proof.proof_id]
    assert miner.refusals == []

    node.error = None
    tick = miner.run_once()
    assert node.accepted == [proof.proof_id]
    assert tick.proofs == (proof.proof_id,)
    assert miner.pending_proofs == {}


def test_a_proof_past_its_claim_window_is_dropped(protocol_accounts: Any) -> None:
    alice, bob = protocol_accounts["alice"], protocol_accounts["bob"]
    relay = _StubRelay(_view(alice.address, bob.address))
    node = _StubNode(error="win from height 19 is older than the 16-block claim window")
    miner = _miner(protocol_accounts, relay, node=node)
    proof = _proof()
    assert not miner._announce_proof(proof)
    assert miner.pending_proofs == {}
    assert miner._flush_proofs() == []


def test_a_proof_the_node_already_holds_is_not_retried(protocol_accounts: Any) -> None:
    alice, bob = protocol_accounts["alice"], protocol_accounts["bob"]
    relay = _StubRelay(_view(alice.address, bob.address))
    node = _StubNode(error="game already has a pending proof")
    miner = _miner(protocol_accounts, relay, node=node)
    proof = _proof()
    assert miner._announce_proof(proof)
    assert miner.pending_proofs == {}
    assert miner._flush_proofs() == []


def test_a_flagged_loss_is_recorded_without_signing_anything(protocol_accounts: Any) -> None:
    alice, bob = protocol_accounts["alice"], protocol_accounts["bob"]
    result = GameResult(
        network=DEVNET.network_id,
        match_id=MATCH_ID,
        move_commitment="00" * 32,
        game_hash="ef" * 32,
        result="0-1",
        termination="time_forfeit",
        plies=len(OPENING) + 1,
        result_signatures={},
    )
    relay = _StubRelay(_view(alice.address, bob.address))
    miner = _miner(protocol_accounts, relay)
    miner.run_once()  # join
    relay.view = relay.view.model_copy(
        update={"status": "finished", "san_moves": (*OPENING, "Nf3"), "result": result}
    )
    tick = miner.run_once()
    assert tick.claims == ()
    assert tick.results == ()
    assert miner.matches[MATCH_ID].finished
    assert miner.claims == {}
    assert any("lost on time" in reason for reason in miner.refusals)
    assert [call for call in relay.calls if call[0] == "result"] == []


def test_close_releases_the_transports(protocol_accounts: Any) -> None:
    alice, bob = protocol_accounts["alice"], protocol_accounts["bob"]
    relay = _StubRelay(_view(alice.address, bob.address))
    miner = _miner(protocol_accounts, relay)
    miner.close()
    assert relay.closed


@dataclass
class _StubPipeline:
    """A pipeline whose submissions and failures the test scripts."""

    actions: tuple[PipelineAction, ...] = ()
    fail: str | None = None
    steps: int = 0

    def step(self) -> tuple[PipelineAction, ...]:
        self.steps += 1
        if self.fail is not None:
            raise EpochPipelineError(self.fail)
        return self.actions


def _pipeline_action(kind: str = "availability", epoch: int = 5) -> PipelineAction:
    return PipelineAction(kind=kind, epoch=epoch, nonce=0, txid="ab" * 32)


def test_the_pipeline_needs_both_a_secret_and_a_node(protocol_accounts: Any) -> None:
    alice, bob = protocol_accounts["alice"], protocol_accounts["bob"]
    relay = _StubRelay(_view(alice.address, bob.address))
    secret = MinerConfig(params=DEVNET, keypair=alice.keypair, nonce_secret=SECRET)
    no_secret = MinerConfig(params=DEVNET, keypair=alice.keypair)

    with_node = EngineMiner(
        secret,
        engine=ScriptedEngine([]),
        relay=relay,  # type: ignore[arg-type]
        node=NodeClient("http://127.0.0.1:1"),
        clock_ms=lambda: 0,
    )
    without_node = EngineMiner(
        secret,
        engine=ScriptedEngine([]),
        relay=relay,  # type: ignore[arg-type]
        clock_ms=lambda: 0,
    )
    unconfigured = EngineMiner(
        no_secret,
        engine=ScriptedEngine([]),
        relay=relay,  # type: ignore[arg-type]
        node=NodeClient("http://127.0.0.1:1"),
        clock_ms=lambda: 0,
    )

    assert with_node.pipeline is not None
    assert with_node.pipeline.address == alice.address
    assert with_node.pipeline.max_games == secret.max_games_per_epoch
    assert with_node.pipeline.nonce_secret == SECRET
    assert without_node.pipeline is None
    assert unconfigured.pipeline is None
    with_node.close()
    without_node.close()
    unconfigured.close()


def test_a_tick_reports_the_pipeline_actions(protocol_accounts: Any) -> None:
    alice, bob = protocol_accounts["alice"], protocol_accounts["bob"]
    relay = _StubRelay(_view(alice.address, bob.address))
    action = _pipeline_action()
    pipeline = _StubPipeline(actions=(action,))
    miner = EngineMiner(
        MinerConfig(params=DEVNET, keypair=alice.keypair),
        engine=ScriptedEngine([]),
        relay=relay,  # type: ignore[arg-type]
        clock_ms=lambda: 0,
        pipeline=pipeline,  # type: ignore[arg-type]
    )

    tick = miner.run_once()

    assert tick.pipeline == (action,)
    assert pipeline.steps == 1
    # The pipeline runs before the relay poll, so the block that carries the
    # transaction can also carry whatever the poll produces.
    assert tick.observed == 1


def test_a_pipeline_failure_is_recorded_as_a_refusal(protocol_accounts: Any) -> None:
    alice, bob = protocol_accounts["alice"], protocol_accounts["bob"]
    relay = _StubRelay(_view(alice.address, bob.address))
    miner = EngineMiner(
        MinerConfig(params=DEVNET, keypair=alice.keypair),
        engine=ScriptedEngine([]),
        relay=relay,  # type: ignore[arg-type]
        clock_ms=lambda: 0,
        pipeline=_StubPipeline(fail="not registered on devnet"),  # type: ignore[arg-type]
    )

    tick = miner.run_once()

    assert tick.pipeline == ()
    assert any("epoch pipeline: not registered" in reason for reason in tick.refusals)
    assert tick.observed == 1


def test_a_relay_outage_keeps_what_the_pipeline_submitted(protocol_accounts: Any) -> None:
    alice = protocol_accounts["alice"]
    relay = _StubRelay(_view(alice.address, "sccp1peerpeerpeer"), fail="connection refused")
    action = _pipeline_action("reveal", 4)
    miner = EngineMiner(
        MinerConfig(params=DEVNET, keypair=alice.keypair),
        engine=ScriptedEngine([]),
        relay=relay,  # type: ignore[arg-type]
        clock_ms=lambda: 0,
        pipeline=_StubPipeline(actions=(action,)),  # type: ignore[arg-type]
    )

    tick = miner.run_once()

    assert tick.observed == 0
    assert tick.pipeline == (action,)
    assert any("relay unavailable" in reason for reason in tick.refusals)
