#!/usr/bin/env python3
"""Play one relay match by hand: you at the keyboard, a miner across the board.

The relay in this repository runs *timed sessions* (plan section 5.7.1): the
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

Two modes:

``python scripts/play_miner.py``
    **Solo.** An in-process relay and one real ``EngineMiner`` are built on the
    devnet parameters with deterministic demo keys, and you play the other seat
    through the relay's HTTP API using the same synchronous ASGI transport
    ``sccp devnet up`` uses. No port is opened and nothing is written to disk.
    The relay's service loop (clock sweeps plus engine polling) runs in a
    background thread, so the clocks really run: sit on your hands for thirty
    minutes and you are flagged, and the engine turns the relay's receipts into
    a verified ``TimeoutClaim``.

``python scripts/play_miner.py --relay http://127.0.0.1:8646 --key alice.key``
    **Attached.** The same human seat, pointed at a relay that is already
    serving (``sccp matchmaker run``), using your wallet keyfile — so you can
    play a seat of a live round against a miner somebody else runs. Open a round
    on that relay first (``POST /v1/rounds``); without one there is nothing to
    join.

Type ``help`` at the prompt for the commands. Moves are accepted as SAN
(``Nf3``, ``O-O``, ``exd5``, ``e8=Q``) or as UCI (``g1f3``).
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import os
import sys
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

import chess
import httpx

from sccp.chess.engines import Engine, open_engine
from sccp.chess.openings import book_for
from sccp.config import NetworkParams, get_network
from sccp.crypto.address import encode_address
from sccp.crypto.keys import Keypair, keypair_from_seed, sign_domain
from sccp.crypto.wallet import load_wallet, read_passphrase_file
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
from sccp.protocol.beacon import commitment, compute_beacon
from sccp.protocol.clocks import sign_move
from sccp.protocol.models import BeaconReveal, Color, Matchup, MoveMessage, TimeControl
from sccp.protocol.pairing import RosterEntry
from sccp.protocol.transcript import certificate_payload
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
"""Demo seed of the engine miner's seat."""

WITNESS_SEED: Final = "c0" * 32
"""Demo seed of the relay's clock witness, so flag receipts can be trusted."""

HUMAN_NONCE: Final = "11" * 32
"""Availability nonce the human seat's beacon commitment is built from."""

OPPONENT_NONCE: Final = "22" * 32
"""Availability nonce the engine miner's beacon commitment is built from."""

HUMAN_ENGINE: Final = "human-v1:0000000000000000"
"""Engine fingerprint of the human seat: honest, and never a real engine."""

SOLO_TIMEOUT_SECONDS: Final = 3600
"""Silence before the relay abandons a seat that never shows up."""

SERVICE_INTERVAL: Final = 0.1
"""Seconds between the solo service loop's miner polls and clock sweeps."""

COLOUR_ATTEMPTS: Final = 8
"""How many demo keys to try before giving up on the requested colour."""

LOW_TIME_MS: Final = 60_000
"""Below this, the prompt warns that the clock is nearly gone."""

EXAMPLES: Final = """\
examples:
  python scripts/play_miner.py                          # solo, you are white
  python scripts/play_miner.py --color black            # solo, you are black
  python scripts/play_miner.py --engine stockfish-19 --engine-path ./bin/stockfish-19
  python scripts/play_miner.py --relay http://127.0.0.1:8646 --key alice.key
"""

HELP_TEXT: Final = """\
moves      type a SAN move (Nf3, O-O, exd5, e8=Q) or a UCI move (g1f3)
board      redraw the position from the relay's move list
moves      print the whole game so far
clocks     print both clocks and whose turn it is
receipts   print the relay's witness receipts for this match
resign     resign on your turn
quit       leave the game (the relay keeps running)
"""


@dataclass(frozen=True, slots=True)
class Options:
    """The validated command line."""

    relay: str | None = None
    key: Path | None = None
    passphrase_file: Path | None = None
    match: str | None = None
    network: str | None = None
    color: str = "white"
    seed: str = HUMAN_SEED
    round: int = 0
    engine: str = "mock"
    engine_path: Path | None = None
    engine_seed: int = 0
    poll_seconds: float = 0.25
    join_seconds: float = 30.0
    ascii: bool = False


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

    def close(self) -> None:
        """Stop the service loop and release the engine and both transports."""
        if self.closed:
            return
        self.closed = True
        self.stop.set()
        if self.thread is not None:
            self.thread.join(timeout=5)
        self.miner.close()
        for client in self.clients:
            client.close()


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
        help=f"Network id: {SOLO_NETWORK} (solo default), testnet or mainnet.",
    )
    parser.add_argument(
        "--color",
        choices=("white", "black", "auto"),
        default="white",
        help="Colour you want in solo mode; 'auto' takes the pairing (default: white).",
    )
    parser.add_argument(
        "--seed",
        metavar="HEX",
        default=HUMAN_SEED,
        help="32-byte hex seed of the solo human seat.",
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
    return Options(
        relay=namespace.relay,
        key=namespace.key,
        passphrase_file=namespace.passphrase_file,
        match=namespace.match,
        network=namespace.network,
        color=namespace.color,
        seed=seed,
        round=namespace.round,
        engine=namespace.engine,
        engine_path=namespace.engine_path,
        engine_seed=namespace.engine_seed,
        poll_seconds=namespace.poll_seconds,
        join_seconds=namespace.join_seconds,
        ascii=namespace.ascii,
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
    reveals = tuple(
        BeaconReveal(miner=address, commitment=commitment(address, SOLO_EPOCH, nonce), nonce=nonce)
        for address, nonce in (
            (human_address, HUMAN_NONCE),
            (opponent_address, OPPONENT_NONCE),
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
    """Yield the seat to play, plus the solo table when this process owns the relay."""
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
        self.out("")
        self.out(f"match    {self.seat.match_id}")
        self.out(f"network  {self.seat.network} epoch {matchup.epoch} round {matchup.round}")
        self.out(f"you      {self.seat.color} as {self.seat.address}")
        self.out(
            f"opponent {_short(self.seat.opponent)} ({_engine_name(view, self.seat.opponent)})"
        )
        self.out(f"clock    {_control_text(view.time_control)}")
        if self.stack is not None:
            self.out("relay    in-process, witness receipts, auto-flag on zero")
        self.out("type 'help' for the commands; moves are SAN or UCI")
        self.out("")

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
            self.out(f"you played {board.san(move)}    {_clock_line(moved)}")
            return moved

    def _ask(self, view: MatchView) -> str:
        """Read one line of input, warning first when the clock is nearly gone."""
        remaining = _remaining_ms(view, self.seat.color)
        if remaining < LOW_TIME_MS:
            self.out(f"low time: {format_clock(remaining)} left for you")
        try:
            return input(f"[{_clock_line(view)}] {self.seat.color}> ").strip()
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
                self.out(f"opponent played {view.san_moves[-1]}    {_clock_line(view)}")
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
            if self.stack.errors:
                self.out(f"relay    {len(self.stack.errors)} service error(s), last: ")
                self.out(f"         {self.stack.errors[-1]}")
        if view.pgn:
            self.out("pgn:")
            for line in view.pgn.splitlines():
                self.out(f"  {line}")
        return 0

    def _settlement(self) -> str:
        """What the opponent miner did with the relay's clock evidence."""
        assert self.stack is not None
        if self.stack.miner.claims:
            return f"settled {len(self.stack.miner.claims)} timeout claim(s) from the receipts"
        if self.stack.miner.refusals:
            return f"refused: {self.stack.miner.refusals[-1]}"
        return "no claim needed"


def main(argv: Sequence[str] | None = None) -> int:
    """Play one game, from the command line to the final report."""
    options = _parse_args(argv)
    try:
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
