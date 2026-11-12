#!/usr/bin/env python3
"""Play one relay match by hand: you at the keyboard, a miner across the board.

The relay in this repository runs *timed sessions* (PROTOCOL.md section 8.4): the
second start signature starts both clocks, every accepted move is receipted by
the relay's clock witness, and a clock that reaches zero flags the session even
if the losing seat never answers again. That is hard to feel from a unit test,
so this script puts a keyboard on one seat:

- your moves are signed with the seat key and posted to
  ``POST /v1/matches/{id}/move`` in the clocked body, so the receipt chain, the
  per-mover charging and the flag path behave exactly as they do between two
  miners;
- everything it renders — board, clocks, receipts, result — comes from the
  relay's ``/v1/matches`` and ``/v1/matches/{id}/receipts`` endpoints.

Three modes:

``python scripts/play_miner.py``
    **Solo.** An in-process relay and one real ``EngineMiner`` are built on the
    devnet parameters with deterministic demo keys, and you play the other seat
    through the relay's HTTP API using the same synchronous ASGI transport
    ``sccp devnet up`` uses. No port is opened, nothing is written to disk, no
    proof is announced to a node and no block is mined: this is an off-chain
    protocol demo, not a miner on a running chain.
    The relay's service loop (clock sweeps plus engine polling) runs in a
    background thread, so the clocks really run: sit on your hands for thirty
    minutes and you are flagged, and the engine turns the relay's receipts into
    a verified ``TimeoutClaim``.

``python scripts/play_miner.py --on-chain``
    **On-chain.** Builds or resumes a local devnet chain in ``--chain-dir``
    (default ``./data-play``), funds and registers your seat and three opponent
    miners, opens the current epoch and then seats you through the same relay
    API. A second run resumes the same chain at its next epoch; ``--fresh``
    discards it, and ``--games N`` plays several games in one sitting. When the
    game ends it co-signs the result, turns the relay transcript into a
    ``WinProof`` and mines that proof into a block with the production block
    builder. The chain is written to SQLite, so ``sccp --data-dir ./data-play
    node run --network devnet`` can serve it and ``scripts/export_block_games.py``
    can export the game. A draw, a time forfeit (there is no claim-transaction
    path in this demo yet) or a game shorter than devnet's 20-ply minimum
    produces no block. A loss is still decisive: the winner's proof is mined,
    whichever seat won.

``python scripts/play_miner.py --relay http://127.0.0.1:8646 --key alice.key``
    **Attached.** The same human seat, pointed at a relay that is already
    serving (``sccp matchmaker run``), using your wallet keyfile — so you can
    play a seat of a live round against a miner somebody else runs. Open a round
    on that relay first (``POST /v1/rounds``); without one there is nothing to
    join.

Type ``help`` at the prompt for the commands. Moves are accepted as SAN
(``Nf3``, ``O-O``, ``exd5``, ``e8=Q``) or as UCI (``g1f3``).

A session never starts at move 1: the relay plays the book line the beacon draws
for this match (``sccp.chess.openings``) and hands you the position after it, so
the first thing this script prints is that line and the board it left. Every
move it reports carries its move number (``3. e4``, ``3... Nd5``), which is the
only way to tell a knight that came from f6 from one that cannot have moved.

The finished game's PGN is printed at the end; ``--pgn-out FILE`` also writes it
to disk. That is the relay's game record, not a win proof. In solo and attached
mode it does not put the game on a chain and it will not appear in
``export_block_games.py``; only ``--on-chain`` mines the proof into a block.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import sys
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Final, cast

import chess
import httpx

from sccp.chain.state import EpochRecord
from sccp.chain.storage import Storage
from sccp.chess.engines import MOCK_VERSION, Engine, open_engine
from sccp.chess.openings import book_for
from sccp.config import NetworkParams, get_network
from sccp.crypto.address import encode_address
from sccp.crypto.keys import Keypair, keypair_from_seed, sign_domain
from sccp.crypto.wallet import load_wallet, read_passphrase_file
from sccp.devnet import (
    GENESIS_EPOCH,
    DevnetMiner,
    _bootstrap,
    _mine_pending,
    _relay_for_epoch,
    _submit_epoch_txs,
)
from sccp.errors import SccpError
from sccp.miner.engine_miner import (
    EMPTY_SIGNATURE,
    EngineMiner,
    MinerConfig,
    RelayClient,
    monotonic_ms,
)
from sccp.miner.matchmaker import ClockBundle, Matchmaker, MatchView
from sccp.miner.matchmaker import create_app as create_relay_app
from sccp.node.service import NodeService
from sccp.protocol.beacon import commitment, compute_beacon
from sccp.protocol.clocks import sign_move
from sccp.protocol.models import (
    BeaconReveal,
    Color,
    Matchup,
    MoveMessage,
    TimeControl,
    Transcript,
)
from sccp.protocol.pairing import RosterEntry
from sccp.protocol.pairing import pair as pair_miners
from sccp.protocol.transcript import (
    certificate_payload,
    proof_from_transcript,
)
from sccp.protocol.transcript import (
    sign_result as sign_game_result,
)
from sccp.serialization import Domain
from sccp.transport import client_for

PASSPHRASE_ENV: Final = "SCCP_WALLET_PASSPHRASE"
"""The variable ``sccp`` itself reads for a keyfile passphrase (see ``sccp.cli``)."""

DEFAULT_RELAY: Final = "http://127.0.0.1:8646"
"""Where ``sccp matchmaker run`` listens by default."""

SOLO_URL: Final = "http://relay"
"""Base URL of the in-process relay; the ASGI transport never dials it."""

SOLO_NETWORK: Final = "devnet"
"""Solo games use devnet parameters: one-block epochs and zero seal difficulty."""

SOLO_EPOCH: Final = 3
"""The first epoch a fresh network can pair in, and so the one the demo uses."""

SOLO_BLOCK_HASH: Final = "ab" * 32
"""A stand-in reveal block hash: the beacon only needs *a* seed here."""

HUMAN_SEED: Final = "5e" * 32
"""Demo seed of the human seat; ``--seed`` overrides it."""

OPPONENT_SEED: Final = "a1" * 32
"""Demo seed of the engine miner's seat in solo mode."""

CHAIN_OPPONENT_SEEDS: Final = ("a1" * 32, "a2" * 32, "a3" * 32)
"""Three chain opponents, so the demo mirrors a mainnet-sized roster.

Test networks disable ``opponent_cooldown``, but rotating distinct, funded
opponents keeps the demo chain close to the shape mainnet enforces -- no two
consecutive proofs recycle one adversary -- and gives ``--color`` a choice of
seats.
"""

WITNESS_SEED: Final = "c0" * 32
"""Demo seed of the relay's clock witness, so flag receipts can be trusted."""

HUMAN_NONCE: Final = "human"
"""Domain tag for the human seat's availability nonce (``solo_nonces`` salts it)."""

OPPONENT_NONCE: Final = "opponent"
"""Domain tag for the engine miner's availability nonce."""

HUMAN_ENGINE: Final = "human-v1:0000000000000000"
"""Engine fingerprint of the human seat: honest, and never a real engine."""

SOLO_TIMEOUT_SECONDS: Final = 3600
"""Silence before the relay abandons a seat that never shows up."""

SERVICE_INTERVAL: Final = 0.1
"""Seconds between the solo service loop's miner polls and clock sweeps."""

COLOUR_ATTEMPTS: Final = 8
"""How many demo keys to try before giving up on the requested colour."""

DEFAULT_CHAIN_DIR: Final = Path("data-play")
"""Where ``--on-chain`` writes the fresh devnet chain, unless ``--chain-dir`` is given."""

CHAIN_DB_NAME: Final = "chain.sqlite"
"""The SQLite file ``sccp node run`` also reads from ``--data-dir``."""

CHAIN_META_NAME: Final = "play.json"
"""A small sidecar recording which demo key owns the chain in ``--chain-dir``."""

CHAIN_META_VERSION: Final = 1
"""Version of :data:`CHAIN_META_NAME`; unknown versions are ignored."""

CHAIN_NETWORK: Final = "devnet"
"""``--on-chain`` is devnet-only: its one-block epochs and zero seal are deterministic."""

MAX_SETTLE_STEPS: Final = 20
"""How many engine polls to spend waiting for the other seat's result signature."""

LOW_TIME_MS: Final = 60_000
"""Below this, the prompt warns that the clock is nearly gone."""

EXAMPLES: Final = """\
examples:
  python scripts/play_miner.py                          # solo, you are white
  python scripts/play_miner.py --color black            # solo, you are black
  python scripts/play_miner.py --on-chain               # play a devnet game, mine the proof
  python scripts/play_miner.py --on-chain --games 3     # three games, three blocks
  python scripts/play_miner.py --on-chain --chain-dir ./data-play
  python scripts/play_miner.py --on-chain --fresh       # discard that chain and bootstrap
  python scripts/play_miner.py --engine stockfish-19 --engine-path ./bin/stockfish-19
  python scripts/play_miner.py --pgn-out game.pgn        # keep the relay's PGN
  python scripts/play_miner.py --relay http://127.0.0.1:8646 --key alice.key
"""

HELP_TEXT: Final = """\
moves      type a SAN move (Nf3, O-O, exd5, e8=Q) or a UCI move (g1f3)
board      redraw the position from the relay's move list
moves      print the whole game so far
clocks     print both clocks and whose turn it is
receipts   print the relay's witness receipts for this match
resign     resign on your turn
quit       leave the game (on-chain: no finished result, so no block)
"""


@dataclass(frozen=True, slots=True)
class Options:
    """The validated command line."""

    relay: str | None = None
    key: Path | None = None
    passphrase_file: Path | None = None
    match: str | None = None
    network: str | None = None
    on_chain: bool = False
    chain_dir: Path | None = None
    fresh: bool = False
    games: int = 1
    color: str = "white"
    seed: str = HUMAN_SEED
    round: int = 0
    engine: str = "mock"
    engine_path: Path | None = None
    engine_seed: int = 0
    poll_seconds: float = 0.25
    join_seconds: float = 30.0
    ascii: bool = False
    pgn_out: Path | None = None


class Quit(Exception):
    """The person typed ``quit`` (or closed stdin)."""


@dataclass(frozen=True, slots=True)
class HumanSeat:
    """One seat of one relay session, driven by the person at the keyboard.

    Everything it sends is what a miner sends: a certificate signature to join,
    then a seat-signed ``MoveMessage`` per move. It never talks to the relay
    directly — :class:`RelayClient` is the same client ``sccp miner run`` uses.
    """

    client: RelayClient
    address: str
    keypair: Keypair
    matchup: Matchup

    @property
    def match_id(self) -> str:
        """The relay's session id (the protocol's match id)."""
        return self.matchup.match_id

    @property
    def network(self) -> str:
        """The network the session was certified on."""
        return self.matchup.network

    @property
    def color(self) -> Color:
        """The colour this seat plays."""
        return "white" if self.matchup.white == self.address else "black"

    @property
    def opponent(self) -> str:
        """The other seat's address."""
        return self.matchup.black if self.color == "white" else self.matchup.white

    def view(self) -> MatchView:
        """The relay's view of this session right now."""
        return self.client.match(self.match_id)

    def join(self, view: MatchView | None = None) -> MatchView:
        """Join the seat by signing the match certificate."""
        current = self.view() if view is None else view
        signature = sign_domain(
            self.keypair, Domain.CERTIFICATE, certificate_payload(current.certificate)
        ).hex()
        return self.client.join(
            self.match_id,
            seat=self.address,
            public_key=self.keypair.public_key_hex,
            signature=signature,
        )

    def play(self, board: chess.Board, move: chess.Move, *, clock_ms: int) -> MatchView:
        """Sign and post one move; ``board`` must already hold every earlier ply.

        ``clock_ms`` is display data: the relay recomputes the clock from its own
        monotonic source and charges the mover, so sending it cannot buy time.
        """
        san = board.san(move)
        message = MoveMessage(
            network=self.network,
            match_id=self.match_id,
            seat=self.address,
            ply=board.ply(),
            san=san,
            clock_ms=clock_ms,
            signature=EMPTY_SIGNATURE,
        )
        signed = sign_move(message, self.keypair)
        return self.client.move(
            self.match_id,
            seat=self.address,
            san=signed.san,
            network=signed.network,
            ply=signed.ply,
            clock_ms=signed.clock_ms,
            signature=signed.signature,
        )

    def resign(self) -> MatchView:
        """Resign on your own turn."""
        return self.client.resign(self.match_id, seat=self.address)

    def receipts(self) -> ClockBundle | None:
        """The relay's clock bundle, or ``None`` for a relay without receipts."""
        return self.client.receipts(self.match_id)

    def cosign_result(self, view: MatchView) -> MatchView:
        """Add this seat's result signature to an already finished match.

        Only ``--on-chain`` needs this: a proof carries the result signatures,
        so the human cannot leave the final result to the engine alone.
        """
        if view.result is None:
            raise SccpError("the relay has not reported a result to co-sign")
        signed = sign_game_result(view.result, view.certificate, self.color, self.keypair)
        return self.client.result(
            self.match_id,
            seat=self.address,
            signature=signed.result_signatures[self.color],
        )

    def transcript(self) -> Transcript:
        """Fetch the relay's final, co-signed transcript."""
        return self.client.transcript(self.match_id)


@dataclass(frozen=True, slots=True)
class ChainSettlement:
    """What happened when the finished game tried to reach a block."""

    mined: bool
    summary: str
    height: int | None = None
    block_hash: str | None = None
    proof_id: str | None = None
    winner: str | None = None
    winner_seat: Color | None = None


@dataclass(slots=True)
class SoloTable:
    """An in-process relay, the opponent miner and the person's seat on it."""

    relay: Matchmaker
    seat: HumanSeat
    miner: EngineMiner
    clients: list[httpx.Client] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    stop: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None
    closed: bool = False
    chain: NodeService | None = None
    chain_dir: Path | None = None
    participants: tuple[DevnetMiner, ...] = ()
    keypairs: dict[str, Keypair] = field(default_factory=dict)
    mined: list[str] = field(default_factory=list)
    prepared: bool = False
    settlement: ChainSettlement | None = None

    def step(self) -> None:
        """One service tick: poll the miner, then sweep the relay's clocks.

        This is what ``sccp matchmaker run``'s timer does, minus the round
        schedule: it is what makes a flag happen without the flagged seat.
        """
        try:
            self.miner.run_once()
            self.relay.sweep()
        except SccpError as exc:  # the relay stays up; the loop reports it
            self.errors.append(str(exc))

    def start(self) -> None:
        """Run :meth:`step` on a daemon thread until :meth:`close`."""
        thread = threading.Thread(target=self._serve, name="solo-relay", daemon=True)
        self.thread = thread
        thread.start()

    def _serve(self) -> None:
        while not self.stop.wait(SERVICE_INTERVAL):
            try:
                self.step()
            except Exception as exc:  # a bad tick must not kill the clock
                self.errors.append(f"{type(exc).__name__}: {exc}")

    def settle(self, view: MatchView) -> ChainSettlement:
        """Co-sign, prove and mine a finished game; idempotent per table."""
        if self.settlement is not None:
            return self.settlement
        if self.chain is None:
            raise SccpError("this table has no chain to settle on")
        self.pause()
        result = view.result
        if result is None:
            self.settlement = ChainSettlement(False, "the relay has not finished the game")
            return self.settlement
        if result.termination == "time_forfeit":
            self.settlement = ChainSettlement(
                False, "time forfeits have no claim transaction in this demo yet"
            )
            return self.settlement
        if result.result == "1/2-1/2":
            self.settlement = ChainSettlement(False, "a draw has no winner to prove")
            return self.settlement
        minimum = self.chain.params.min_plies
        if result.plies < minimum:
            self.settlement = ChainSettlement(
                False,
                f"{result.plies} plies is below devnet's {minimum}-ply proof minimum",
            )
            return self.settlement
        try:
            self._prepare_next_epoch()
            signed_view = self._co_sign_result(view)
            if signed_view is None:
                self.settlement = ChainSettlement(False, self._missing_signature_reason())
                return self.settlement
            transcript = self.seat.transcript()
            proof = proof_from_transcript(transcript, params=self.chain.params)
            if proof.proof_id not in self.chain.mempool.proofs:
                self.chain.announce_proof(proof)
            if not _mine_pending(self.chain, self.keypairs, self.mined):
                self.settlement = ChainSettlement(
                    False, "the pending proof had no winner key to mine it"
                )
                return self.settlement
            stored = self.chain.chain.block_at_height(self.chain.height)
            if stored is None or not any(
                item.proof_id == proof.proof_id for item in stored.block.proofs
            ):
                self.settlement = ChainSettlement(False, "the new block does not contain the proof")
                return self.settlement
            self.settlement = ChainSettlement(
                True,
                f"block {stored.height} {stored.block_hash[:12]}…",
                height=stored.height,
                block_hash=stored.block_hash,
                proof_id=proof.proof_id,
                winner=proof.winner,
                winner_seat=proof.winner_seat,
            )
        except SccpError as exc:
            self.settlement = ChainSettlement(False, f"settlement failed: {exc}")
        return self.settlement

    def _co_sign_result(self, view: MatchView) -> MatchView | None:
        """Wait for both result signatures, adding the human's if it is missing.

        The engine signs on its next poll. The human signs here because there is
        no input loop left to do it: the prompt returned as soon as the game
        ended. Returns ``None`` if the signatures never both arrive.
        """
        for _ in range(MAX_SETTLE_STEPS):
            result = view.result
            if result is None:
                return None
            signatures = set(result.result_signatures)
            missing = {"white", "black"} - signatures
            if not missing:
                return view
            if self.seat.color in missing:
                view = self.seat.cosign_result(view)
                continue
            try:
                self.miner.run_once()
            except SccpError as exc:
                self.errors.append(str(exc))
                return None
            view = self.seat.view()
        return None

    def _prepare_next_epoch(self) -> None:
        """Add the availability/reveal transactions the *next* game will need.

        Devnet epochs are one block long. A proof from epoch ``e`` is mined at
        height ``e + 1``, so the next game needs epoch ``e + 1`` to be matchable
        by then. The block that carries this game's proof also carries those
        transactions, which is what lets a resumed chain host another game.
        """
        if self.prepared or self.chain is None or not self.participants:
            return
        _submit_epoch_txs(self.chain, self.participants)
        self.prepared = True

    def _missing_signature_reason(self) -> str:
        """Explain a co-sign failure with the opponent's own refusal, if it has one."""
        result = self.seat.view().result
        present = "none" if result is None else ", ".join(sorted(result.result_signatures))
        if result is None:
            return "the relay no longer reports a result to co-sign"
        if self.miner.refusals:
            return f"the opponent refused to co-sign: {self.miner.refusals[-1]}"
        return f"both seats did not co-sign the result (signatures: {present or 'none'})"

    def pause(self) -> None:
        """Stop the background service loop so settlement owns the miner.

        ``EngineMiner`` is deliberately single-threaded; co-signing and mining
        must not race the thread that was polling it during the game.
        """
        self.stop.set()
        thread = self.thread
        if thread is not None:
            thread.join(timeout=5)
            self.thread = None

    def close(self) -> None:
        """Stop the service loop and release the chain, engine and transports."""
        if self.closed:
            return
        self.closed = True
        self.stop.set()
        if self.thread is not None:
            self.thread.join(timeout=5)
        self.miner.close()
        for client in self.clients:
            client.close()
        if self.chain is not None:
            self.chain.close()


def _parse_args(argv: Sequence[str] | None) -> Options:
    """Parse and validate the command line."""
    parser = argparse.ArgumentParser(
        prog="play_miner.py",
        description="Play one timed relay match by hand against a miner.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=EXAMPLES,
    )
    parser.add_argument(
        "--relay",
        metavar="URL",
        help="Attach to a running relay instead of building one in-process.",
    )
    parser.add_argument(
        "--key",
        type=Path,
        metavar="FILE",
        help="Wallet keyfile of the seat you play (attached mode).",
    )
    parser.add_argument(
        "--passphrase-file",
        type=Path,
        metavar="FILE",
        help="File holding the keyfile passphrase (mode 0600, first line used).",
    )
    parser.add_argument(
        "--match",
        metavar="ID",
        help="Play this match id instead of the first open seat of --key.",
    )
    parser.add_argument(
        "--network",
        metavar="ID",
        help=f"Network id: {SOLO_NETWORK} (solo/on-chain default), testnet or mainnet.",
    )
    parser.add_argument(
        "--on-chain",
        action="store_true",
        help=(
            "Build or resume a devnet chain and mine the finished game's proof into a "
            "block (writes --chain-dir; --fresh discards an existing one)."
        ),
    )
    parser.add_argument(
        "--chain-dir",
        type=Path,
        metavar="DIR",
        help=f"Data directory for --on-chain (default: ./{DEFAULT_CHAIN_DIR}).",
    )
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="With --on-chain, discard an existing chain in --chain-dir and bootstrap again.",
    )
    parser.add_argument(
        "--games",
        type=int,
        default=1,
        metavar="N",
        help="Play N games in a row on the same chain (on-chain only, default: 1).",
    )
    parser.add_argument(
        "--color",
        choices=("white", "black", "auto"),
        default="white",
        help=("Colour you want in solo/on-chain mode; 'auto' takes the pairing (default: white)."),
    )
    parser.add_argument(
        "--seed",
        metavar="HEX",
        default=HUMAN_SEED,
        help="32-byte hex seed of the solo/on-chain human seat.",
    )
    parser.add_argument(
        "--round", type=int, default=0, metavar="N", help="Round to open in solo mode."
    )
    parser.add_argument(
        "--engine",
        default="mock",
        metavar="PROFILE",
        help="Engine profile the opponent miner runs: mock, stockfish-19, stockfish-18.",
    )
    parser.add_argument(
        "--engine-path",
        type=Path,
        metavar="FILE",
        help="Engine binary; required for every profile except 'mock'.",
    )
    parser.add_argument(
        "--engine-seed",
        type=int,
        default=0,
        metavar="N",
        help="Seed of the mock engine's choice, for reproducible games.",
    )
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=0.25,
        metavar="F",
        help="How often to poll the relay while waiting (default: 0.25).",
    )
    parser.add_argument(
        "--join-seconds",
        type=float,
        default=30.0,
        metavar="F",
        help="How long to wait for the opponent to join (default: 30).",
    )
    parser.add_argument(
        "--ascii",
        action="store_true",
        help="Print the board as ASCII instead of Unicode.",
    )
    parser.add_argument(
        "--pgn-out",
        type=Path,
        metavar="FILE",
        help="Write the finished game's PGN to FILE (the relay's record, not a proof).",
    )
    namespace = parser.parse_args(argv)
    seed = str(namespace.seed).lower()
    if len(seed) != 64 or any(char not in "0123456789abcdef" for char in seed):
        parser.error("--seed must be 32 bytes of hex (64 characters)")
    if namespace.poll_seconds <= 0:
        parser.error("--poll-seconds must be positive")
    if namespace.join_seconds <= 0:
        parser.error("--join-seconds must be positive")
    if namespace.round < 0:
        parser.error("--round cannot be negative")
    if namespace.relay is not None and namespace.key is None:
        parser.error("--relay needs --key: a seat signs its own certificate and moves")
    if namespace.on_chain and namespace.relay is not None:
        parser.error("--on-chain builds its own devnet; it cannot attach to --relay")
    if namespace.on_chain and namespace.key is not None:
        parser.error("--on-chain derives the human seat from --seed; drop --key")
    if namespace.on_chain and namespace.match is not None:
        parser.error("--on-chain opens its own round; drop --match")
    if namespace.chain_dir is not None and not namespace.on_chain:
        parser.error("--chain-dir only applies to --on-chain")
    if namespace.fresh and not namespace.on_chain:
        parser.error("--fresh only applies to --on-chain")
    if namespace.games < 1:
        parser.error("--games must be at least 1")
    if namespace.games > 1 and not namespace.on_chain:
        parser.error("playing several games in a row needs --on-chain")
    if namespace.games > 1 and namespace.pgn_out is not None:
        parser.error("--pgn-out names one game; leave it off when --games is greater than 1")
    if namespace.on_chain and namespace.network not in (None, CHAIN_NETWORK):
        parser.error(f"--on-chain is devnet-only; drop --network {namespace.network}")
    return Options(
        relay=namespace.relay,
        key=namespace.key,
        passphrase_file=namespace.passphrase_file,
        match=namespace.match,
        network=namespace.network,
        on_chain=namespace.on_chain,
        chain_dir=namespace.chain_dir,
        fresh=namespace.fresh,
        games=namespace.games,
        color=namespace.color,
        seed=seed,
        round=namespace.round,
        engine=namespace.engine,
        engine_path=namespace.engine_path,
        engine_seed=namespace.engine_seed,
        poll_seconds=namespace.poll_seconds,
        join_seconds=namespace.join_seconds,
        ascii=namespace.ascii,
        pgn_out=namespace.pgn_out,
    )


def _passphrase(options: Options) -> str:
    """The keyfile passphrase: ``--passphrase-file``, the environment, or a prompt."""
    if options.passphrase_file is not None:
        return read_passphrase_file(options.passphrase_file)
    from_env = os.environ.get(PASSPHRASE_ENV)
    if from_env:
        return from_env
    return getpass.getpass("Passphrase: ")


def choose_session(
    views: Sequence[MatchView], address: str, match_id: str | None = None
) -> MatchView | None:
    """The session to play: the requested id, else the first open seat of ``address``."""
    for view in views:
        if address not in (view.matchup.white, view.matchup.black):
            continue
        if match_id is not None:
            if view.matchup.match_id == match_id:
                return view
        elif view.status in ("pending", "active"):
            return view
    return None


def solo_nonces(seed: str) -> tuple[str, str]:
    """The two availability nonces of a solo table, derived from ``--seed``.

    The beacon is a pure function of the reveals and the opening is a pure
    function of the beacon, so deriving the nonces from the seed is what makes
    ``--seed`` draw a *different* certified opening: without it every solo game
    would start from the same book line.
    """
    base = bytes.fromhex(seed)
    human = hashlib.sha256(base + HUMAN_NONCE.encode()).hexdigest()
    opponent = hashlib.sha256(base + OPPONENT_NONCE.encode()).hexdigest()
    return human, opponent


def chain_opponents() -> tuple[Keypair, ...]:
    """The deterministic opponent keys a chain bootstraps."""
    return tuple(keypair_from_seed(bytes.fromhex(seed)) for seed in CHAIN_OPPONENT_SEEDS)


def chain_opponent_nonces(seed: str) -> tuple[str, ...]:
    """One availability nonce per chain opponent, all derived from ``--seed``."""
    base = bytes.fromhex(seed)
    return tuple(
        hashlib.sha256(base + f"{OPPONENT_NONCE}:{index}".encode()).hexdigest()
        for index in range(len(CHAIN_OPPONENT_SEEDS))
    )


def _solo(options: Options) -> SoloTable:
    """Build the solo table, retrying demo keys until the pairing seats you as asked."""
    params = get_network(options.network or SOLO_NETWORK)
    base = bytes.fromhex(options.seed)
    for attempt in range(COLOUR_ATTEMPTS):
        seed = base if attempt == 0 else hashlib.sha256(base + attempt.to_bytes(4, "big")).digest()
        table = _solo_table(params, keypair_from_seed(seed), options)
        if options.color in ("auto", table.seat.color):
            return table
        table.close()
    raise SystemExit(
        f"could not be seated as {options.color} in {COLOUR_ATTEMPTS} tries; "
        "use --color auto or a different --seed"
    )


def _solo_table(
    params: NetworkParams,
    human: Keypair,
    options: Options,
    *,
    clock_ms: Callable[[], int] = monotonic_ms,
    engine: Engine | None = None,
) -> SoloTable:
    """One relay, two seats and one real miner, all in this process.

    The beacon is derived the way a chain derives it — from two availability
    reveals — so the pairing, the match id and the certified opening are the
    ones a verifier would recompute. The relay runs with a witness key, so its
    receipts are signed and the opponent miner can settle a flag it wins.
    """
    engine = (
        engine
        if engine is not None
        else open_engine(options.engine, binary=options.engine_path, seed=options.engine_seed)
    )
    opponent = keypair_from_seed(bytes.fromhex(OPPONENT_SEED))
    witness = keypair_from_seed(bytes.fromhex(WITNESS_SEED))
    human_address = encode_address(human.public_key_bytes, params)
    opponent_address = encode_address(opponent.public_key_bytes, params)
    witness_address = encode_address(witness.public_key_bytes, params)
    human_nonce, opponent_nonce = solo_nonces(options.seed)
    reveals = tuple(
        BeaconReveal(miner=address, commitment=commitment(address, SOLO_EPOCH, nonce), nonce=nonce)
        for address, nonce in (
            (human_address, human_nonce),
            (opponent_address, opponent_nonce),
        )
    )
    beacon = compute_beacon(
        network=params.network_id,
        epoch=SOLO_EPOCH,
        block_hash=SOLO_BLOCK_HASH,
        reveals=reveals,
    )
    relay = Matchmaker(
        params,
        beacon=beacon,
        roster={
            address: RosterEntry(address=address, max_games=params.rounds_per_epoch)
            for address in (human_address, opponent_address)
        },
        public_keys={
            human_address: human.public_key_hex,
            opponent_address: opponent.public_key_hex,
        },
        engines={human_address: HUMAN_ENGINE, opponent_address: engine.fingerprint},
        book=book_for(params),
        clock_ms=clock_ms,
        timeout_seconds=SOLO_TIMEOUT_SECONDS,
        witness_keypair=witness,
    )
    relay.open_round(options.round)
    app = create_relay_app(relay)
    human_http = client_for(app, base_url=SOLO_URL)
    miner_http = client_for(app, base_url=SOLO_URL)
    seat = HumanSeat(
        client=RelayClient(SOLO_URL, client=human_http),
        address=human_address,
        keypair=human,
        matchup=_solo_matchup(relay, human_address),
    )
    config = MinerConfig(
        params=params,
        keypair=opponent,
        relay_url=SOLO_URL,
        max_games_per_epoch=params.rounds_per_epoch,
        witness_keys=((witness_address, witness.public_key_hex),),
    )
    miner = EngineMiner(
        config,
        engine=engine,
        relay=RelayClient(SOLO_URL, client=miner_http),
        clock_ms=clock_ms,
    )
    return SoloTable(relay=relay, seat=seat, miner=miner, clients=[human_http, miner_http])


def _solo_matchup(relay: Matchmaker, address: str) -> Matchup:
    """The one pairing of the open round that contains ``address``."""
    for view in relay.matches():
        if address in (view.matchup.white, view.matchup.black):
            return view.matchup
    raise SystemExit(f"the relay paired no match containing {address}")


def _seat_matchup(
    record: EpochRecord,
    human_address: str,
    options: Options,
    *,
    opponent_addresses: set[str],
    blocked_opponents: set[str],
    params: NetworkParams,
    epoch: int,
) -> Matchup:
    """Choose a pairing that contains the human and an available opponent.

    Any round of the epoch is equally valid to the chain: the certificate and
    the proof carry the match, not the round number. Pairings are deterministic,
    so computing them before opening a round means the engine never sees a
    discarded session.
    """
    if record.beacon is None:  # the caller checks this for a clearer error
        raise SystemExit(f"epoch {epoch} has no beacon to pair in")
    roster = [
        RosterEntry(address=address, max_games=member.max_games)
        for address, member in record.roster.items()
    ]
    rounds = [options.round]
    rounds.extend(number for number in range(params.rounds_per_epoch) if number != options.round)
    fallback: Matchup | None = None
    for round_number in rounds:
        matchups = pair_miners(
            roster,
            bytes.fromhex(record.beacon.hash),
            network=params.network_id,
            epoch=epoch,
            round_number=round_number,
            rounds_per_epoch=params.rounds_per_epoch,
            opponent_cooldown=params.opponent_cooldown,
        )
        for matchup in matchups:
            if human_address not in (matchup.white, matchup.black):
                continue
            opponent = matchup.black if human_address == matchup.white else matchup.white
            if opponent not in opponent_addresses or opponent in blocked_opponents:
                continue
            if fallback is None:
                fallback = matchup
            if options.color == "auto":
                return matchup
            if (human_address == matchup.white) == (options.color == "white"):
                return matchup
    if fallback is not None:
        return fallback
    raise SystemExit(
        f"this chain has no available opponent that can seat you as {options.color}; "
        "use --color auto or --fresh"
    )


@dataclass(frozen=True, slots=True)
class _HumanFingerprint:
    """The fake engine identity a human seat registers on chain."""

    fingerprint: str


def _chain(options: Options) -> SoloTable:
    """Build or resume the devnet chain, seating you in the requested colour."""
    params = get_network(options.network or SOLO_NETWORK)
    if params.network_id != CHAIN_NETWORK:
        raise SystemExit(
            f"--on-chain is devnet-only; got --network {params.network_id} "
            "(devnet's one-block epochs and zero seal are what make this demo deterministic)"
        )
    data_dir = options.chain_dir or DEFAULT_CHAIN_DIR
    database = data_dir / CHAIN_DB_NAME
    if database.exists() and not options.fresh:
        return _resume_chain(params, options, data_dir)
    if options.fresh:
        _remove_chain(database)
        _remove_chain_meta(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    base = bytes.fromhex(options.seed)
    for attempt in range(COLOUR_ATTEMPTS):
        if attempt:
            _remove_chain(database)
        seed = base if attempt == 0 else hashlib.sha256(base + attempt.to_bytes(4, "big")).digest()
        table = _chain_table(params, keypair_from_seed(seed), options)
        if options.color in ("auto", table.seat.color):
            _write_chain_meta(data_dir, seed)
            return table
        table.close()
    _remove_chain(database)
    raise SystemExit(
        f"could not be seated as {options.color} in {COLOUR_ATTEMPTS} tries; "
        "use --color auto or a different --seed"
    )


def _resume_chain(params: NetworkParams, options: Options, data_dir: Path) -> SoloTable:
    """Open the chain in ``data_dir`` again and seat the person on its next epoch."""
    database = data_dir / CHAIN_DB_NAME
    meta = _read_chain_meta(data_dir)
    recorded_network = meta.get("network") if meta is not None else None
    if recorded_network is not None and recorded_network != params.network_id:
        raise SystemExit(
            f"{database} is a {recorded_network!r} chain, not {params.network_id!r}; "
            "use a different --chain-dir"
        )
    service = NodeService.open(params, storage=Storage(database))
    try:
        service.check_startup()
        found = _human_keypair_for_chain(service, options, meta)
        if found is None:
            raise SystemExit(
                f"{database} already exists, but it has no human seat derived from --seed. "
                "Pass the --seed used to create it, or use --fresh to start over."
            )
        seed, human = found
        epoch = _next_chain_epoch(service)
        table = _chain_table(params, human, options, service=service, epoch=epoch)
    except BaseException:
        service.close()
        raise
    if options.color not in ("auto", table.seat.color):
        table.close()
        raise SystemExit(
            f"the next epoch of this chain does not seat you as {options.color}; "
            "use --color auto, or start a new chain with --fresh"
        )
    _write_chain_meta(data_dir, seed)
    return table


def _next_chain_epoch(service: NodeService) -> int:
    """The epoch the next game must use: the tip's own, and it must be matchable."""
    state = service.chain.state
    epoch = state.current_epoch()
    record = state.epoch_record(epoch)
    if record is None or not record.matchable:
        raise SystemExit(
            f"the chain tip at height {state.height} has no matchable epoch {epoch} roster; "
            "use --fresh to start a new devnet"
        )
    return epoch


def _candidate_seeds(options: Options, meta: dict[str, object] | None) -> list[bytes]:
    """The stored seed first, then every seed this command line could have used."""
    seeds: list[bytes] = []
    recorded = meta.get("human_seed") if meta is not None else None
    if isinstance(recorded, str):
        try:
            seed = bytes.fromhex(recorded)
        except ValueError:
            seed = b""
        if len(seed) == 32:
            seeds.append(seed)
    base = bytes.fromhex(options.seed)
    for attempt in range(COLOUR_ATTEMPTS):
        seed = base if attempt == 0 else hashlib.sha256(base + attempt.to_bytes(4, "big")).digest()
        if seed not in seeds:
            seeds.append(seed)
    return seeds


def _human_keypair_for_chain(
    service: NodeService, options: Options, meta: dict[str, object] | None
) -> tuple[bytes, Keypair] | None:
    """Find the human key already registered on a resumed chain."""
    state = service.chain.state
    human_addresses = {
        address
        for address, account in state.accounts.items()
        if account.engine == HUMAN_ENGINE and account.public_key is not None
    }
    for seed in _candidate_seeds(options, meta):
        keypair = keypair_from_seed(seed)
        if encode_address(keypair.public_key_bytes, service.params) in human_addresses:
            return seed, keypair
    return None


def _chain_meta_path(data_dir: Path) -> Path:
    """Where a chain records the demo key that owns its human seat."""
    return data_dir / CHAIN_META_NAME


def _read_chain_meta(data_dir: Path) -> dict[str, object] | None:
    """Read the sidecar, treating an old or malformed one as absent."""
    path = _chain_meta_path(data_dir)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(document, dict) or document.get("version") != CHAIN_META_VERSION:
        return None
    return document


def _write_chain_meta(data_dir: Path, seed: bytes) -> None:
    """Remember which seed to use the next time this chain is resumed."""
    payload = {
        "version": CHAIN_META_VERSION,
        "network": CHAIN_NETWORK,
        "human_seed": seed.hex(),
    }
    with suppress(OSError):
        _chain_meta_path(data_dir).write_text(
            json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8"
        )


def _remove_chain(database: Path) -> None:
    """Remove the SQLite files a discarded colour attempt wrote."""
    for suffix in ("", "-wal", "-shm"):
        Path(f"{database}{suffix}").unlink(missing_ok=True)


def _remove_chain_meta(data_dir: Path) -> None:
    """Remove the sidecar that belongs to a discarded chain."""
    _chain_meta_path(data_dir).unlink(missing_ok=True)


def _chain_table(
    params: NetworkParams,
    human: Keypair,
    options: Options,
    *,
    clock_ms: Callable[[], int] = monotonic_ms,
    engine: Engine | None = None,
    service: NodeService | None = None,
    epoch: int = GENESIS_EPOCH,
) -> SoloTable:
    """A devnet chain (fresh or resumed) plus the relay, miner and seat on top of it.

    This is the library path ``sccp devnet up`` uses: ``_bootstrap`` funds and
    registers both seats, ``_relay_for_epoch`` builds the real relay for
    ``epoch`` (beacon, pairing, receipts), and the chain is persisted in
    ``chain.sqlite`` so ``sccp node run`` can serve it after the game.
    """
    engine = (
        engine
        if engine is not None
        else open_engine(options.engine, binary=options.engine_path, seed=options.engine_seed)
    )
    opponents = chain_opponents()
    opponent_nonces = chain_opponent_nonces(options.seed)
    witness = keypair_from_seed(bytes.fromhex(WITNESS_SEED))
    human_address = encode_address(human.public_key_bytes, params)
    witness_address = encode_address(witness.public_key_bytes, params)
    human_nonce, _ = solo_nonces(options.seed)
    opponents_by_address = {
        encode_address(opponent.public_key_bytes, params): opponent for opponent in opponents
    }
    participants = (
        DevnetMiner(
            keypair=human,
            engine=cast(Engine, _HumanFingerprint(HUMAN_ENGINE)),
            nonce_secret=human_nonce,
        ),
        *(
            DevnetMiner(keypair=opponent, engine=engine, nonce_secret=nonce)
            for opponent, nonce in zip(opponents, opponent_nonces, strict=True)
        ),
    )
    data_dir = options.chain_dir or DEFAULT_CHAIN_DIR
    data_dir.mkdir(parents=True, exist_ok=True)
    owns_service = service is None
    if service is None:
        database = data_dir / CHAIN_DB_NAME
        service = NodeService.open(params, storage=Storage(database))
    clients: list[httpx.Client] = []
    try:
        service.check_startup()
        if owns_service:
            _bootstrap(service, participants)
        record = service.chain.state.epoch_record(epoch)
        if record is None or record.beacon is None:  # _relay_for_epoch checks this too
            raise SystemExit(f"epoch {epoch} has no beacon to pair in")
        active = tuple(
            participant for participant in participants if participant.address in record.roster
        )
        opponent_addresses = set(opponents_by_address) & set(record.roster)
        stats = service.chain.state.miner_stats.get(human_address)
        blocked: set[str] = set()
        if stats is not None and params.opponent_cooldown > 0:
            blocked = {
                opponent
                for opponent, last_epoch in stats.last_opponent_epoch.items()
                if epoch - last_epoch <= params.opponent_cooldown
            }
        matchup = _seat_matchup(
            record,
            human_address,
            options,
            opponent_addresses=opponent_addresses,
            blocked_opponents=blocked,
            params=params,
            epoch=epoch,
        )
        opponent_address = matchup.black if human_address == matchup.white else matchup.white
        opponent = opponents_by_address[opponent_address]
        pair = _relay_for_epoch(
            service,
            epoch,
            active,
            clock=lambda: clock_ms() // 1000,
            clock_ms=clock_ms,
            timeout_seconds=SOLO_TIMEOUT_SECONDS,
            witness_keypair=witness,
        )
        pair.relay.open_round(matchup.round)
        human_http = client_for(create_relay_app(pair.relay), base_url=SOLO_URL)
        clients.append(human_http)
        clients.extend(pair.http)
        seat = HumanSeat(
            client=RelayClient(SOLO_URL, client=human_http),
            address=human_address,
            keypair=human,
            matchup=matchup,
        )
        config = MinerConfig(
            params=params,
            keypair=opponent,
            relay_url=SOLO_URL,
            max_games_per_epoch=params.rounds_per_epoch,
            witness_keys=((witness_address, witness.public_key_hex),),
        )
        miner = EngineMiner(
            config,
            engine=engine,
            relay=pair.relay_client,
            clock_ms=clock_ms,
        )
    except BaseException:
        for client in clients:
            client.close()
        if owns_service:
            service.close()
        raise
    return SoloTable(
        relay=pair.relay,
        seat=seat,
        miner=miner,
        clients=clients,
        chain=service,
        chain_dir=data_dir,
        participants=active,
        keypairs={human_address: human, **opponents_by_address},
    )


def _attached(options: Options) -> HumanSeat:
    """Load the seat keyfile and pick the session to play on a running relay."""
    if options.key is None:  # argparse rejects this; keeps the type honest
        raise SystemExit("--relay needs --key")
    wallet = load_wallet(options.key, _passphrase(options))
    params = get_network(options.network or wallet.network)
    if params.network_id != wallet.network:
        raise SystemExit(
            f"keyfile is for {wallet.network!r}, but --network {params.network_id!r} was requested"
        )
    relay_url = options.relay or DEFAULT_RELAY
    client = RelayClient(relay_url)
    try:
        views = client.matches()
    except SccpError as exc:
        client.close()
        raise SystemExit(f"cannot reach the relay at {relay_url}: {exc}") from None
    view = choose_session(views, wallet.address, options.match)
    if view is None:
        client.close()
        raise SystemExit(
            f"the relay at {relay_url} has no open seat for {wallet.address}. "
            "Open a round there first, e.g. "
            f"curl -s -X POST {relay_url}/v1/rounds "
            '-H "content-type: application/json" -d \'{"round": 0}\''
        )
    return HumanSeat(
        client=client,
        address=wallet.address,
        keypair=wallet.keypair,
        matchup=view.matchup,
    )


@contextmanager
def _seat(options: Options) -> Iterator[tuple[HumanSeat, SoloTable | None]]:
    """Yield the seat to play, plus the local table when this process owns the relay."""
    if options.on_chain:
        table = _chain(options)
        table.start()
        try:
            yield table.seat, table
        finally:
            table.close()
        return
    if options.relay is not None:
        seat = _attached(options)
        try:
            yield seat, None
        finally:
            seat.client.close()
        return
    table = _solo(options)
    table.start()
    try:
        yield table.seat, table
    finally:
        table.close()


def format_clock(ms: int) -> str:
    """``mm:ss.t`` for one clock, clamped at zero."""
    remaining = max(ms, 0)
    seconds, millis = divmod(remaining, 1_000)
    minutes, seconds = divmod(seconds, 60)
    return f"{minutes:02d}:{seconds:02d}.{millis // 100}"


def _control_text(control: TimeControl) -> str:
    """``30+0``, with the units spelled out for a player rather than a bot."""
    return f"{control.initial_ms // 60_000}+{control.increment_ms // 1_000} per side"


def _clock_line(view: MatchView) -> str:
    """Both clocks, with a ``*`` on the side to move."""
    running = view.status == "active"
    white = "*" if running and view.turn == "white" else " "
    black = "*" if running and view.turn == "black" else " "
    return f"white {format_clock(view.white_ms)}{white}  black {format_clock(view.black_ms)}{black}"


def _remaining_ms(view: MatchView, color: Color) -> int:
    """The seat's clock as the relay last reported it."""
    return view.white_ms if color == "white" else view.black_ms


def _is_our_turn(view: MatchView, color: Color) -> bool:
    """Whether this seat is the one on the clock."""
    return view.status == "active" and view.turn == color


def _board_from(view: MatchView) -> chess.Board:
    """A board holding every ply the relay has accepted."""
    board = chess.Board()
    for san in view.san_moves:
        board.push(board.parse_san(san))
    return board


def numbered_moves(sans: Sequence[str]) -> list[str]:
    """PGN-style move rows: ``1. e4 e5``, one row per move number."""
    rows: list[str] = []
    for ply, san in enumerate(sans):
        if ply % 2 == 0:
            rows.append(f"{ply // 2 + 1:>3}. {san}")
        else:
            rows[-1] = f"{rows[-1]} {san}"
    return rows


def move_label(ply: int) -> str:
    """``3.`` in front of white's move, ``3...`` in front of black's.

    ``ply`` is the zero-based index of the half-move, so the label of the move
    about to be played in a position is ``move_label(board.ply())``.
    """
    number, side = divmod(ply, 2)
    return f"{number + 1}." if side == 0 else f"{number + 1}..."


def move_text(sans: Sequence[str]) -> str:
    """One line of PGN move text: ``1. c4 e5 2. Nc3 Nf6``."""
    return " ".join(row.strip() for row in numbered_moves(sans))


def parse_move(board: chess.Board, text: str) -> chess.Move | None:
    """Read a SAN or UCI move from human input; ``None`` if it is not legal.

    ``0-0`` is normalised to ``O-O`` because a keyboard makes the difference
    invisible, and a trailing ``+``, ``#``, ``!`` or ``?`` is optional: the move
    is what gets checked, not the annotation.
    """
    cleaned = text.strip().replace("0-0-0", "O-O-O").replace("0-0", "O-O").rstrip("+#!?")
    if not cleaned:
        return None
    try:
        return board.parse_san(cleaned)
    except ValueError:
        pass
    try:
        move = chess.Move.from_uci(cleaned)
    except ValueError:
        return None
    return move if board.is_legal(move) else None


def _engine_name(view: MatchView, address: str) -> str:
    """The engine fingerprint the certificate pinned for one seat."""
    if address == view.matchup.white:
        return view.certificate.engine_white
    return view.certificate.engine_black


def _verdict(result: str, color: Color) -> str:
    """``won``, ``lost`` or ``drew``, from one seat's point of view."""
    if result == "1/2-1/2":
        return "drew"
    winner = "white" if result == "1-0" else "black"
    return "won" if winner == color else "lost"


def _short(address: str) -> str:
    """Enough of an address to recognise a seat, without a wall of bech32."""
    return f"{address[:16]}…"


class Game:
    """The interactive loop: render what the relay says, prompt, post, wait."""

    def __init__(
        self,
        seat: HumanSeat,
        *,
        stack: SoloTable | None,
        options: Options,
        out: Callable[[str], None] = print,
    ) -> None:
        self.seat = seat
        self.stack = stack
        self.options = options
        self.out = out

    def run(self) -> int:
        """Play until the session finishes; returns the process exit code."""
        view = self.seat.view()
        self._banner(view)
        if self.seat.color not in view.certificate.start_signatures:
            view = self.seat.join(view)
            self.out(f"joined as {self.seat.color}")
        view = self._wait(
            lambda candidate: candidate.status != "pending",
            "the opponent to join",
            self.options.join_seconds,
        )
        if view.status == "pending":
            self.out("the opponent never joined, so the session never started")
            return 1
        if view.san_moves:
            self._announce_opening(view)
        while True:
            view = self.seat.view()
            if view.status == "finished":
                break
            if view.status != "active":
                self.out(f"the session is {view.status}; stopping")
                return 1
            if _is_our_turn(view, self.seat.color):
                view = self._turn(view)
            else:
                view = self._await_opponent(view)
        return self._report(view)

    # -- the loop ----------------------------------------------------------

    def _banner(self, view: MatchView) -> None:
        """Say who is who, and on which terms, before the first prompt."""
        matchup = self.seat.matchup
        engine = _engine_name(view, self.seat.opponent)
        self.out("")
        self.out(f"match    {self.seat.match_id}")
        self.out(f"network  {self.seat.network} epoch {matchup.epoch} round {matchup.round}")
        self.out(f"you      {self.seat.color} as {self.seat.address}")
        self.out(f"opponent {_short(self.seat.opponent)} ({engine})")
        self.out(f"clock    {_control_text(view.time_control)}")
        if self.stack is not None:
            self.out("relay    in-process, witness receipts, auto-flag on zero")
            if self.stack.chain is not None:
                self.out(
                    f"chain    devnet at {self.stack.chain_dir} (height {self.stack.chain.height})"
                )
                self.out("         the finished result is co-signed, proved and mined here")
                self.out("         another run resumes this chain; --fresh resets it")
            else:
                self.out("chain    off-chain demo: no proof announced, no block mined")
        if engine.startswith(MOCK_VERSION):
            self.out("engine   seeded random legal moves, not a real engine:")
            self.out("         --engine stockfish-19 --engine-path BINARY to play one")
        self.out("type 'help' for the commands; moves are SAN or UCI")
        self.out("")

    def _announce_opening(self, view: MatchView) -> None:
        """Show the certified book line the game started from, and the position.

        The relay plays the opening the book draws for this beacon and match id
        before the first prompt; those plies are neither yours nor receipted, so
        a player who is never told starts from a board they have not seen and
        reads the next knight move as an illegal one.
        """
        board = _board_from(view)
        self.out("")
        self.out(f"opening  {move_text(view.san_moves)}")
        self.out("         certified book plies: no clock, no receipts, not your moves")
        self.out(f"you play move {board.fullmove_number} as {self.seat.color}")
        self._show_board(board)

    def _wait(
        self,
        predicate: Callable[[MatchView], bool],
        what: str,
        seconds: float,
    ) -> MatchView:
        """Poll until ``predicate`` holds or ``seconds`` pass; return the last view."""
        view = self.seat.view()
        if predicate(view):
            return view
        self.out(f"waiting for {what}…")
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            time.sleep(self.options.poll_seconds)
            view = self.seat.view()
            if predicate(view):
                return view
        return view

    def _turn(self, view: MatchView) -> MatchView:
        """Prompt until the relay accepts a move or a resignation, then return."""
        board = _board_from(view)
        while True:
            raw = self._ask(view)
            command = raw.lower()
            if not command:
                continue
            if command in ("help", "h", "?", "commands"):
                self.out(HELP_TEXT)
                continue
            if command in ("board", "b"):
                self._show_board(board)
                continue
            if command in ("moves", "m"):
                self._show_moves(view)
                continue
            if command in ("clock", "clocks", "c"):
                self.out(_clock_line(view))
                continue
            if command == "receipts":
                self._show_receipts()
                continue
            if command in ("quit", "exit", "q"):
                raise Quit
            if command in ("resign", "r"):
                return self._resign()
            move = parse_move(board, raw)
            if move is None:
                self.out(f"{raw!r} is not a legal move in this position")
                continue
            try:
                moved = self.seat.play(board, move, clock_ms=_remaining_ms(view, self.seat.color))
            except SccpError as exc:
                self.out(f"the relay refused the move: {exc}")
                view = self.seat.view()
                if view.status != "active":
                    return view
                board = _board_from(view)
                continue
            self.out(
                f"you played {move_label(board.ply())}{board.san(move)}    {_clock_line(moved)}"
            )
            return moved

    def _ask(self, view: MatchView) -> str:
        """Read one line of input, warning first when the clock is nearly gone."""
        remaining = _remaining_ms(view, self.seat.color)
        if remaining < LOW_TIME_MS:
            self.out(f"low time: {format_clock(remaining)} left for you")
        label = move_label(len(view.san_moves))
        try:
            return input(f"[{_clock_line(view)}] {label} {self.seat.color}> ").strip()
        except EOFError:
            raise Quit from None

    def _resign(self) -> MatchView:
        """Resign, or say why the relay would not accept it."""
        try:
            resigned = self.seat.resign()
        except SccpError as exc:
            self.out(f"the relay refused the resignation: {exc}")
            return self.seat.view()
        self.out(f"you resigned    {_clock_line(resigned)}")
        return resigned

    def _await_opponent(self, view: MatchView) -> MatchView:
        """Poll while the opponent is on the clock, announcing each reply."""
        last_ply = len(view.san_moves)
        started = time.monotonic()
        noted = started
        self.out(f"{_short(self.seat.opponent)} is thinking… ({_clock_line(view)})")
        while view.status == "active" and not _is_our_turn(view, self.seat.color):
            time.sleep(self.options.poll_seconds)
            view = self.seat.view()
            if len(view.san_moves) > last_ply:
                label = move_label(len(view.san_moves) - 1)
                self.out(f"opponent played {label}{view.san_moves[-1]}    {_clock_line(view)}")
                last_ply = len(view.san_moves)
            elif time.monotonic() - noted >= 15:
                # Their clock is the one running, so this is information, not a
                # stall we have to break out of: Ctrl-C is always available.
                noted = time.monotonic()
                self.out(f"still waiting ({_clock_line(view)}, {time.monotonic() - started:.0f}s)")
        return view

    # -- rendering ---------------------------------------------------------

    def _show_board(self, board: chess.Board) -> None:
        """Draw the position, from your side of the table."""
        if self.options.ascii:
            self.out(str(board))
            # ``str(board)`` has no file labels; without them a plain ASCII
            # board cannot be read for a move like ``Bb5``.
            self.out("  a b c d e f g h")
            return
        self.out(board.unicode(borders=True, invert_color=self.seat.color == "black"))

    def _show_moves(self, view: MatchView) -> None:
        """Print the whole game so far, one move number per row."""
        if not view.san_moves:
            self.out("no moves yet")
            return
        self.out("\n".join(numbered_moves(view.san_moves)))

    def _show_receipts(self) -> None:
        """Print the relay's clock bundle: the evidence a timeout claim carries."""
        bundle = self.seat.receipts()
        if bundle is None:
            self.out("this relay does not publish receipts")
            return
        if not bundle.receipts:
            self.out("no receipts yet (opening plies are certified, not receipted)")
        for receipt in bundle.receipts:
            self.out(
                f"ply {receipt.ply:>3}  {receipt.san:<7} "
                f"w {format_clock(receipt.white_ms)}  b {format_clock(receipt.black_ms)}  "
                f"witnesses {len(receipt.witnesses)}"
            )
        if bundle.flag is not None:
            self.out(
                f"flag ply {bundle.flag.ply:>3}  seat {_short(bundle.flag.seat)}  "
                f"witnesses {len(bundle.flag.witnesses)}"
            )

    # -- the report --------------------------------------------------------

    def _report(self, view: MatchView) -> int:
        """Summarise the finished session; the result is the relay's, not ours."""
        result = view.result
        self.out("")
        self.out("=== finished ===")
        if result is None:
            self.out(f"the relay reports {view.status} with no result")
            return 1
        verdict = _verdict(result.result, self.seat.color)
        self.out(
            f"result   {result.result} by {result.termination} "
            f"after {result.plies} plies: you {verdict}"
        )
        self.out(
            f"clocks   white {format_clock(view.white_ms)}  black {format_clock(view.black_ms)}"
        )
        if view.flagged is not None:
            self.out(f"flagged  {view.flagged}: its clock reached zero")
        bundle = self.seat.receipts()
        if bundle is not None:
            signed = (
                "signed" if any(receipt.witnesses for receipt in bundle.receipts) else "unsigned"
            )
            self.out(f"receipts {len(bundle.receipts)} move receipts, {signed}")
        if self.stack is not None:
            self.out(f"engine   {self._settlement()}")
            if self.stack.chain is not None:
                self.out("chain    co-signing the result and mining the proof…")
                settlement = self.stack.settle(view)
                if settlement.mined:
                    self.out(f"chain    mined {settlement.summary}")
                    self.out(
                        f"proof    {settlement.proof_id} "
                        f"(winner {settlement.winner_seat}: {settlement.winner}, "
                        f"network {result.network})"
                    )
                    self.out(
                        f"node     sccp --data-dir {self.stack.chain_dir} node run --network devnet"
                    )
                else:
                    self.out(f"chain    not mined: {settlement.summary}")
                    self.out(
                        f"         chain stays at height {self.stack.chain.height}; "
                        "a decisive game is required for a proof"
                    )
            if self.stack.errors:
                self.out(f"relay    {len(self.stack.errors)} service error(s), last: ")
                self.out(f"         {self.stack.errors[-1]}")
        if view.pgn:
            self.out("pgn:")
            for line in view.pgn.splitlines():
                self.out(f"  {line}")
            if self.options.pgn_out is not None and not self._save_pgn(view.pgn):
                return 1
        return 0

    def _save_pgn(self, pgn: str) -> bool:
        """Write the relay's PGN; report and return ``False`` if it cannot."""
        path = self.options.pgn_out
        assert path is not None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(pgn, encoding="utf-8")
        except OSError as exc:
            self.out(f"error: cannot write {path}: {exc}")
            return False
        self.out(f"pgn file {path}")
        return True

    def _settlement(self) -> str:
        """What the opponent miner did with the relay's clock evidence."""
        assert self.stack is not None
        if self.stack.miner.claims:
            return f"settled {len(self.stack.miner.claims)} timeout claim(s) from the receipts"
        if self.stack.miner.refusals:
            return f"refused: {self.stack.miner.refusals[-1]}"
        return "no claim needed"


def _play_chain_games(options: Options) -> int:
    """Play ``--games`` games in a row, reopening the same chain between them."""
    code = 0
    for index in range(options.games):
        run_options = replace(options, fresh=options.fresh if index == 0 else False)
        with _seat(run_options) as (seat, stack):
            code = Game(seat, stack=stack, options=run_options).run()
        if code != 0:
            return code
    return code


def main(argv: Sequence[str] | None = None) -> int:
    """Play one game, from the command line to the final report."""
    options = _parse_args(argv)
    try:
        if options.on_chain and options.games > 1:
            return _play_chain_games(options)
        with _seat(options) as (seat, stack):
            return Game(seat, stack=stack, options=options).run()
    except Quit:
        print("\nleft the game", file=sys.stderr)
        return 0
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    except SccpError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
