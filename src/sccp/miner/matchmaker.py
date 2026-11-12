"""The matchmaking relay (FastAPI), sections 5.7 and 5.7.1.

The relay is a convenience service, not a trusted party. It derives pairings
from the beacon, opens a session per matchup, relays SAN moves after checking
them, and stores both seats' signatures. Every object it returns can be
recomputed and checked by anyone with the roster and the beacon.

Sessions are **timed**: each live match carries the protocol's fixed 30+0 clock
(PROTOCOL.md section 8.4). The relay starts both clocks when the second seat joins,
deducts the mover's elapsed time on every accepted move, signs a receipt for it
with its witness key, and flags a clock that reaches zero even if the losing
seat never sends another message. Moves may carry the clocked body
``{network, match_id, seat, ply, san, clock_ms, signature}``; when a signature
is present the relay verifies it against the registered seat key, so the move
stream a verifier later replays is signed by the seats rather than trusted from
the relay.

State lives in :class:`Matchmaker` and is protected by a re-entrant lock; the
HTTP layer is a thin translation of the same calls. A JSON store (optional)
makes restart safe: sessions are serialised with their SAN list, clock and
receipts, so pending matches keep their position and finished transcripts
survive.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, TypeVar, cast

import chess
from fastapi import FastAPI, HTTPException
from pydantic import Field

from sccp.chess.openings import OpeningBook, book_for
from sccp.chess.pgn import GameResult as PgnResult
from sccp.chess.pgn import Termination, canonical_game_hash, outcome_result
from sccp.config import NetworkParams, get_network
from sccp.crypto.address import encode_address
from sccp.crypto.keys import Keypair, public_key_from_hex, verify_domain
from sccp.errors import ClockError, KeyEncodingError, RelayError
from sccp.protocol.clocks import (
    DEFAULT_LAG_GRACE_MS,
    FIXED_TIME_CONTROL,
    MatchClock,
    flag_result,
    sign_receipt,
    verify_move_signature,
)
from sccp.protocol.models import (
    MAX_CLOCK_MS,
    AddressText,
    Beacon,
    Color,
    FlagReceipt,
    GameResult,
    MatchCertificate,
    Matchup,
    MoveMessage,
    MoveReceipt,
    ProtocolModel,
    PublicKeyHex,
    ResultValue,
    RosterMember,
    SignatureHex,
    TerminationValue,
    TimeControl,
    Transcript,
)
from sccp.protocol.pairing import RosterEntry, pair
from sccp.protocol.transcript import (
    certificate_payload,
    create_certificate,
    render_transcript_pgn,
    result_payload,
)
from sccp.serialization import Domain

STORE_FORMAT = 2
"""Relay store version. Format 2 adds clocks, receipts and round history."""

TIMED_SESSIONS = True
"""The relay always runs timed sessions; the flag is here for documentation."""


def relay_monotonic_ms() -> int:
    """The relay's authoritative millisecond clock (injected in tests).

    Monotonic rather than wall-clock: a system time step must not create or
    destroy a player's time. Receipt timestamps are only ever compared to each
    other, never to a client's timestamp.
    """
    return int(time.monotonic() * 1000)


T = TypeVar("T")
"""Return type of the HTTP error-translation helper."""


class SessionStatus(StrEnum):
    """Lifecycle of one relay session."""

    PENDING = "pending"
    ACTIVE = "active"
    FINISHED = "finished"
    ABANDONED = "abandoned"


class JoinRequest(ProtocolModel):
    """A seat joins a match by signing its certificate."""

    seat: AddressText
    public_key: PublicKeyHex
    signature: SignatureHex


class MoveRequest(ProtocolModel):
    """A seat submits one SAN move.

    The untimed body is ``{seat, san}``; the timed body adds ``network``, ``ply``,
    ``clock_ms`` and ``signature``. ``clock_ms`` is display data and is never
    trusted: the relay recomputes the clock from its own monotonic source. A
    body that carries a ``signature`` must also carry ``network`` and ``ply``,
    and the signature is verified against the registered seat key.
    """

    seat: AddressText
    san: str = Field(min_length=1, max_length=16)
    network: str | None = Field(default=None, min_length=1, max_length=32)
    ply: int | None = Field(default=None, ge=0)
    clock_ms: int | None = Field(default=None, ge=0, le=MAX_CLOCK_MS)
    signature: SignatureHex | None = None

    @property
    def signed(self) -> bool:
        """Whether the body is the clocked, seat-signed form."""
        return self.signature is not None


class ResignRequest(ProtocolModel):
    """A seat resigns on its turn."""

    seat: AddressText


class ResultRequest(ProtocolModel):
    """A seat co-signs the result."""

    seat: AddressText
    signature: SignatureHex


class RoundRequest(ProtocolModel):
    """Ask the relay to open the next (or a given) round."""

    round: int = Field(ge=0)


class MatchView(ProtocolModel):
    """The public state of a session, including its live clocks.

    ``white_ms``/``black_ms`` are the relay's authoritative view at response
    time; ``turn`` and ``last_ply`` say whose clock is running and how many
    plies (including the opening prefix) have been played. ``flagged`` is set
    once a clock reached zero, and ``receipts`` counts the signed move receipts
    the relay holds (fetch ``/v1/matches/{id}/receipts`` for the objects).
    """

    matchup: Matchup
    status: str
    san_moves: tuple[str, ...]
    certificate: MatchCertificate
    result: GameResult | None = None
    pgn: str | None = None
    time_control: TimeControl = Field(default_factory=lambda: FIXED_TIME_CONTROL)
    white_ms: int = Field(default=FIXED_TIME_CONTROL.initial_ms, ge=0, le=MAX_CLOCK_MS)
    black_ms: int = Field(default=FIXED_TIME_CONTROL.initial_ms, ge=0, le=MAX_CLOCK_MS)
    turn: Color = "white"
    last_ply: int = Field(default=0, ge=0)
    flagged: Color | None = None
    receipts: int = Field(default=0, ge=0)


class ClockBundle(ProtocolModel):
    """Everything the relay observed about one match's clock.

    ``receipts`` covers every post-opening ply in order, so the tuple is exactly
    the ``clock_evidence.receipts`` list of a timeout claim. ``flag`` is the
    signed flag receipt once a clock reached zero. Both are witness-signed by
    the relay when it runs with a witness key, and the receipts carry no witness
    signatures at all when it does not (a relay may observe without attesting).
    """

    match_id: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    time_control: TimeControl
    turn: Color
    white_ms: int = Field(ge=0, le=MAX_CLOCK_MS)
    black_ms: int = Field(ge=0, le=MAX_CLOCK_MS)
    last_ply: int = Field(ge=0)
    started: bool
    receipts: tuple[MoveReceipt, ...] = ()
    flag: FlagReceipt | None = None


def _clock_document(clock: MatchClock | None) -> dict[str, object] | None:
    """Serialize a live clock for the JSON store."""
    if clock is None:
        return None
    return {
        "white_ms": clock.white_ms,
        "black_ms": clock.black_ms,
        "turn": clock.turn,
        "turn_started_at_ms": clock.turn_started_at_ms,
        "lag_grace_ms": clock.lag_grace_ms,
        "started": clock.started,
    }


def _clock_from_document(document: object, control: TimeControl) -> MatchClock | None:
    """Rebuild a clock from the JSON store, rejecting nonsense values."""
    if document is None:
        return None
    if not isinstance(document, Mapping):
        raise RelayError("stored match clock is malformed")
    turn = document.get("turn")
    if turn not in ("white", "black"):
        raise RelayError("stored match clock has an invalid turn")
    try:
        return MatchClock(
            control=control,
            white_ms=int(document["white_ms"]),
            black_ms=int(document["black_ms"]),
            turn=turn,
            turn_started_at_ms=int(document["turn_started_at_ms"]),
            lag_grace_ms=int(document.get("lag_grace_ms", DEFAULT_LAG_GRACE_MS)),
            started=bool(document.get("started", True)),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise RelayError(f"stored match clock is invalid: {exc}") from None


@dataclass(slots=True)
class MatchSession:
    """One relayed game. ``san_moves`` includes the opening prefix."""

    matchup: Matchup
    certificate: MatchCertificate
    board: chess.Board
    san_moves: list[str]
    status: SessionStatus
    created_at: int
    last_seen_at: int
    result: GameResult | None = None
    pgn: str | None = None
    clock: MatchClock | None = None
    receipts: list[MoveReceipt] = field(default_factory=list)
    flag: FlagReceipt | None = None

    @property
    def turn(self) -> Color:
        """The colour whose clock is running."""
        return "white" if self.board.turn == chess.WHITE else "black"

    @property
    def flagged(self) -> Color | None:
        """The colour a flag receipt named, if any."""
        if self.flag is None:
            return None
        return "white" if self.flag.seat == self.certificate.white else "black"

    def snapshot(self, now_ms: int) -> dict[Color, int]:
        """Remaining clocks at ``now_ms`` (both at the initial value if unstarted)."""
        if self.clock is None:
            initial = self.certificate.time_control.initial_ms
            return {"white": initial, "black": initial}
        return self.clock.snapshot(now_ms)

    def view(self, now_ms: int) -> MatchView:
        clocks = self.snapshot(now_ms)
        return MatchView(
            matchup=self.matchup,
            status=self.status.value,
            san_moves=tuple(self.san_moves),
            certificate=self.certificate,
            result=self.result,
            pgn=self.pgn,
            time_control=self.certificate.time_control,
            white_ms=clocks["white"],
            black_ms=clocks["black"],
            turn=self.turn,
            last_ply=len(self.san_moves),
            flagged=self.flagged,
            receipts=len(self.receipts),
        )

    def to_document(self) -> dict[str, object]:
        return {
            "matchup": self.matchup.model_dump(mode="json"),
            "certificate": self.certificate.model_dump(mode="json"),
            "san_moves": list(self.san_moves),
            "status": self.status.value,
            "created_at": self.created_at,
            "last_seen_at": self.last_seen_at,
            "result": self.result.model_dump(mode="json") if self.result else None,
            "pgn": self.pgn,
            "clock": _clock_document(self.clock),
            "receipts": [receipt.model_dump(mode="json") for receipt in self.receipts],
            "flag": self.flag.model_dump(mode="json") if self.flag else None,
        }

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> MatchSession:
        """Rebuild a session, replaying its SAN list to recover the board."""
        try:
            matchup = Matchup.model_validate(document["matchup"])
            certificate = MatchCertificate.model_validate(document["certificate"])
            status = SessionStatus(document["status"])
            san_moves = [str(item) for item in document["san_moves"]]
            created_at = int(document["created_at"])
            last_seen_at = int(document["last_seen_at"])
            raw_result = document.get("result")
            result = GameResult.model_validate(raw_result) if raw_result else None
            raw_pgn = document.get("pgn")
            pgn = str(raw_pgn) if raw_pgn is not None else None
            clock = _clock_from_document(document.get("clock"), certificate.time_control)
            raw_receipts = document.get("receipts") or []
            if not isinstance(raw_receipts, list):
                raise TypeError("receipts must be a list")
            receipts = [MoveReceipt.model_validate(item) for item in raw_receipts]
            raw_flag = document.get("flag")
            flag = FlagReceipt.model_validate(raw_flag) if raw_flag else None
        except (KeyError, TypeError, ValueError) as exc:
            raise RelayError(f"stored match session is invalid: {exc}") from None

        board = chess.Board()
        try:
            for san in san_moves:
                board.push(board.parse_san(san))
        except ValueError as exc:
            raise RelayError(f"stored match {matchup.match_id} replays badly: {exc}") from None
        prefix = tuple(san_moves[: len(certificate.opening.moves)])
        if prefix != certificate.opening.moves:
            raise RelayError(
                f"stored match {matchup.match_id} does not follow its certificate opening"
            )
        if status is SessionStatus.FINISHED and result is None:
            raise RelayError(f"stored match {matchup.match_id} is finished without a result")
        if status in (SessionStatus.PENDING, SessionStatus.ACTIVE) and board.is_game_over(
            claim_draw=False
        ):
            raise RelayError(f"stored match {matchup.match_id} is open but already over")
        if certificate.match_id != matchup.match_id:
            raise RelayError(f"stored match {matchup.match_id} has a certificate for another match")
        if certificate.white != matchup.white or certificate.black != matchup.black:
            raise RelayError(f"stored match {matchup.match_id} has mismatched certificate seats")
        if status is SessionStatus.FINISHED:
            if result is None or pgn is None:
                raise RelayError(
                    f"stored match {matchup.match_id} is finished without a result or PGN"
                )
            try:
                expected_hash = canonical_game_hash(matchup.network, san_moves, result.result)
            except ValueError as exc:
                raise RelayError(
                    f"stored match {matchup.match_id} has an invalid result: {exc}"
                ) from None
            if expected_hash != result.game_hash:
                raise RelayError(f"stored match {matchup.match_id} has a mismatched game hash")
            if result.termination not in ("resignation", "time_forfeit"):
                outcome = board.outcome(claim_draw=False)
                if outcome is None:
                    raise RelayError(
                        f"stored match {matchup.match_id} claims a terminal result "
                        "but the board is not over"
                    )
                computed_result, computed_termination = outcome_result(outcome)
                if (
                    computed_result.value != result.result
                    or computed_termination.value != result.termination
                ):
                    raise RelayError(
                        f"stored match {matchup.match_id} result disagrees with the board"
                    )
            elif (
                result.termination == "resignation" and board.outcome(claim_draw=False) is not None
            ):
                raise RelayError(
                    f"stored match {matchup.match_id} claims resignation on a finished board"
                )
        elif result is not None:
            raise RelayError(f"stored match {matchup.match_id} is not finished but has a result")

        opening_plies = len(certificate.opening.moves)
        if [receipt.ply for receipt in receipts] != list(range(opening_plies, len(san_moves))):
            raise RelayError(
                f"stored match {matchup.match_id} does not carry one receipt per post-opening ply"
            )
        for receipt in receipts:
            if receipt.match_id != matchup.match_id:
                raise RelayError(f"stored match {matchup.match_id} has a receipt for another match")
        if flag is not None:
            if flag.match_id != matchup.match_id:
                raise RelayError(f"stored match {matchup.match_id} has a flag for another match")
            if flag.seat not in (certificate.white, certificate.black):
                raise RelayError(f"stored match {matchup.match_id} flagged a non-seat")
            if flag.ply != len(san_moves):
                raise RelayError(
                    f"stored match {matchup.match_id} flags ply {flag.ply}, "
                    f"but the game has {len(san_moves)} plies"
                )
            if status is not SessionStatus.FINISHED or result is None:
                raise RelayError(f"stored match {matchup.match_id} has a flag but is not finished")
            flagged: Color = "white" if flag.seat == certificate.white else "black"
            if flag_result(board, flagged=flagged) != result.result:
                raise RelayError(
                    f"stored match {matchup.match_id} flag result disagrees with the board"
                )
        elif (
            status is SessionStatus.FINISHED
            and result is not None
            and result.termination == "time_forfeit"
        ):
            raise RelayError(
                f"stored match {matchup.match_id} is a time forfeit without a flag receipt"
            )
        return cls(
            matchup=matchup,
            certificate=certificate,
            board=board,
            san_moves=san_moves,
            status=status,
            created_at=created_at,
            last_seen_at=last_seen_at,
            result=result,
            pgn=pgn,
            clock=clock,
            receipts=receipts,
            flag=flag,
        )


def _seat_colour(session: MatchSession, seat: str) -> Color:
    if seat == session.certificate.white:
        return "white"
    if seat == session.certificate.black:
        return "black"
    raise RelayError(f"{seat} is not a seat in match {session.matchup.match_id}")


class Matchmaker:
    """Epoch relay state: roster, beacon, sessions and reputation.

    All mutation methods take the internal lock, so the synchronous core is safe
    to call from FastAPI's threadpool or directly in tests.
    """

    def __init__(
        self,
        params: NetworkParams,
        *,
        beacon: Beacon,
        roster: Mapping[str, RosterEntry] | Sequence[RosterEntry],
        public_keys: Mapping[str, str],
        engines: Mapping[str, str],
        book: OpeningBook | None = None,
        store: Path | None = None,
        clock: Callable[[], int] | None = None,
        clock_ms: Callable[[], int] | None = None,
        timeout_seconds: int = 300,
        witness_keypair: Keypair | None = None,
        lag_grace_ms: int | None = None,
    ) -> None:
        if beacon.network != params.network_id:
            raise RelayError(f"beacon is for {beacon.network!r}, relay is on {params.network_id!r}")
        if timeout_seconds < 1:
            raise RelayError("timeout_seconds must be at least 1")
        grace = params.lag_grace_ms if lag_grace_ms is None else lag_grace_ms
        if not 0 <= grace <= 1_000:
            raise RelayError("lag_grace_ms must be in [0, 1000]")

        entries = list(roster.values()) if isinstance(roster, Mapping) else list(roster)
        self._roster: dict[str, RosterEntry] = {}
        for entry in entries:
            if entry.address in self._roster:
                raise RelayError(f"duplicate roster address {entry.address}")
            self._roster[entry.address] = entry

        missing_keys = sorted(set(self._roster) - set(public_keys))
        if missing_keys:
            raise RelayError(f"no registered public key for {missing_keys}")
        missing_engines = sorted(set(self._roster) - set(engines))
        if missing_engines:
            raise RelayError(f"no engine fingerprint for {missing_engines}")
        for address in self._roster:
            key_hex = public_keys[address]
            try:
                raw_key = bytes.fromhex(key_hex)
            except ValueError:
                raise RelayError(f"public key for {address} is not valid hex") from None
            if encode_address(raw_key, params) != address:
                raise RelayError(f"public key for {address} derives a different address")
            if not engines[address]:
                raise RelayError(f"engine fingerprint for {address} is empty")

        self.params = params
        self.beacon = beacon
        self.book = book if book is not None else book_for(params)
        self._public_keys = dict(public_keys)
        self._engines = dict(engines)
        self._store = store
        self._clock = clock or (lambda: int(time.time()))
        self._clock_ms = clock_ms or relay_monotonic_ms
        self.timeout_seconds = timeout_seconds
        self.witness_keypair = witness_keypair
        self.lag_grace_ms = grace
        self.round_seconds: float = 0.0
        """Relay policy: seconds between scheduled rounds (0 disables ``--round-seconds``)."""
        self._sessions: dict[str, MatchSession] = {}
        self._no_shows: dict[str, int] = {}
        self._rounds_opened: dict[int, int] = {}
        self._lock = threading.RLock()
        self._load()

    @property
    def witness(self) -> str | None:
        """The address whose signatures the relay puts on its receipts."""
        if self.witness_keypair is None:
            return None
        return encode_address(self.witness_keypair.public_key_bytes, self.params)

    def _now_ms(self) -> int:
        return int(self._clock_ms())

    # -- persistence ---------------------------------------------------------

    def _load(self) -> None:
        if self._store is None or not self._store.exists():
            return
        try:
            document = json.loads(self._store.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RelayError(f"cannot read relay store {self._store}: {exc}") from None
        if not isinstance(document, dict) or document.get("format") not in (1, STORE_FORMAT):
            raise RelayError(f"relay store {self._store} has an unsupported format")
        if document.get("network") != self.params.network_id:
            raise RelayError(
                f"relay store {self._store} belongs to network {document.get('network')!r}"
            )
        now_ms = self._now_ms()
        raw_sessions = document.get("sessions", [])
        if not isinstance(raw_sessions, list):
            raise RelayError(f"relay store {self._store} has a malformed session list")
        for raw in raw_sessions:
            if not isinstance(raw, Mapping):
                raise RelayError(f"relay store {self._store} has a malformed session")
            session = MatchSession.from_document(raw)
            if session.matchup.network != self.params.network_id:
                raise RelayError(f"stored match {session.matchup.match_id} is for another network")
            self._sessions[session.matchup.match_id] = session
        raw_no_shows = document.get("no_shows", {})
        if not isinstance(raw_no_shows, dict):
            raise RelayError(f"relay store {self._store} has malformed reputation data")
        for address, count in raw_no_shows.items():
            if not isinstance(address, str) or not isinstance(count, int) or count < 0:
                raise RelayError(f"relay store {self._store} has malformed reputation data")
            self._no_shows[address] = count
        raw_rounds = document.get("rounds", {})
        if not isinstance(raw_rounds, dict):
            raise RelayError(f"relay store {self._store} has malformed round history")
        for key, opened_at in raw_rounds.items():
            if not isinstance(key, str) or not isinstance(opened_at, int) or opened_at < 0:
                raise RelayError(f"relay store {self._store} has malformed round history")
            self._rounds_opened[int(key)] = opened_at
        # A live clock's turn boundary comes from the previous process's
        # monotonic clock, which means nothing here: give the side to move its
        # current turn again rather than flagging it on restart.
        for session in self._sessions.values():
            self._resume_clock_locked(session, now_ms)

    def _persist_locked(self) -> None:
        if self._store is None:
            return
        document = {
            "format": STORE_FORMAT,
            "network": self.params.network_id,
            "sessions": [
                session.to_document()
                for session in sorted(
                    self._sessions.values(), key=lambda item: item.matchup.match_id
                )
            ],
            "no_shows": dict(sorted(self._no_shows.items())),
            "rounds": {
                str(number): opened_at for number, opened_at in sorted(self._rounds_opened.items())
            },
        }
        try:
            self._store.parent.mkdir(parents=True, exist_ok=True)
            temporary = self._store.with_name(self._store.name + ".tmp")
            temporary.write_text(
                json.dumps(document, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )
            temporary.replace(self._store)
        except OSError as exc:
            raise RelayError(f"cannot write relay store {self._store}: {exc}") from None

    # -- helpers -------------------------------------------------------------

    def _now(self) -> int:
        return int(self._clock())

    def _require_session(self, match_id: str) -> MatchSession:
        session = self._sessions.get(match_id)
        if session is None:
            raise RelayError(f"unknown match {match_id}")
        return session

    def _expire_locked(self) -> list[str]:
        """Abandon silent joins. A live clocked session is never abandoned.

        A clocked match ends on its clock (PROTOCOL.md section 8.4), so the
        inactivity reaper only applies to sessions that have no running clock:
        pending joins, and the theoretically impossible active-but-unclocked
        state. Anything else would let a losing seat deny its opponent the flag.
        """
        now = self._now()
        expired: list[str] = []
        for session in self._sessions.values():
            if session.status not in (SessionStatus.PENDING, SessionStatus.ACTIVE):
                continue
            if session.status is SessionStatus.ACTIVE and session.clock is not None:
                continue
            if now - session.last_seen_at <= self.timeout_seconds:
                continue
            previous = session.status
            session.status = SessionStatus.ABANDONED
            expired.append(session.matchup.match_id)
            if previous is SessionStatus.PENDING:
                joined = set(session.certificate.start_signatures)
                missing = [
                    address
                    for colour, address in (
                        ("white", session.certificate.white),
                        ("black", session.certificate.black),
                    )
                    if colour not in joined
                ]
            else:
                missing = [session.certificate.white, session.certificate.black]
            for seat in missing:
                self._no_shows[seat] = self._no_shows.get(seat, 0) + 1
        return expired

    def _start_clock_locked(self, session: MatchSession, now_ms: int) -> MatchClock:
        """Start both clocks with the board's side to move on the clock."""
        clock = MatchClock.start(
            session.certificate.time_control,
            now_ms=now_ms,
            lag_grace_ms=self.lag_grace_ms,
        )
        clock.turn = session.turn
        session.clock = clock
        return clock

    def _resume_clock_locked(self, session: MatchSession, now_ms: int) -> None:
        """Restart the turn boundary of a live clock after a relay restart."""
        if session.status is not SessionStatus.ACTIVE:
            return
        if session.clock is None:
            self._start_clock_locked(session, now_ms)
            return
        session.clock.turn = session.turn
        session.clock.turn_started_at_ms = now_ms
        session.clock.lag_grace_ms = self.lag_grace_ms
        session.clock.started = True

    def _sign_receipt_locked(self, receipt: MoveReceipt | FlagReceipt) -> MoveReceipt | FlagReceipt:
        """Add the relay's witness signature, when the relay has a key."""
        if self.witness_keypair is None:
            return receipt
        return sign_receipt(
            receipt,
            self.witness_keypair,
            network=self.params.network_id,
            witness=self.witness,
        )

    def _flag_locked(self, session: MatchSession, now_ms: int) -> bool:
        """Flag a seat whose clock reached zero and finish the match.

        Returns ``False`` when no clock has run out. The flag receipt is signed
        by the relay's witness key, so the opponent can settle the game on chain
        with a bonded ``timeout_claim`` even though the flagged seat never
        answered (PROTOCOL.md sections 8.4 and 10.4).
        """
        clock = session.clock
        if session.status is not SessionStatus.ACTIVE or clock is None:
            return False
        flagged = clock.flag(now_ms)
        if flagged is None:
            return False
        clocks = clock.snapshot(now_ms)
        seat = session.certificate.white if flagged == "white" else session.certificate.black
        receipt = FlagReceipt(
            match_id=session.matchup.match_id,
            ply=len(session.san_moves),
            seat=seat,
            received_at_ms=now_ms,
            white_ms=0 if flagged == "white" else clocks["white"],
            black_ms=0 if flagged == "black" else clocks["black"],
        )
        signed = self._sign_receipt_locked(receipt)
        assert isinstance(signed, FlagReceipt)
        session.flag = signed
        clock.stop(now_ms)
        result = flag_result(session.board, flagged=flagged)
        outcome = PgnResult(result)
        self._finish_locked(session, outcome, Termination.TIME_FORFEIT)
        return True

    def _sweep_locked(self, now_ms: int) -> bool:
        """Flag expired clocks and reap silent joins. Returns whether state moved."""
        changed = bool(self._expire_locked())
        for session in list(self._sessions.values()):
            if self._flag_locked(session, now_ms):
                changed = True
        return changed

    def _finish_locked(
        self,
        session: MatchSession,
        result: PgnResult,
        termination: Termination,
        *,
        now_ms: int | None = None,
    ) -> None:
        if now_ms is not None and session.clock is not None:
            session.clock.stop(now_ms)
        session.pgn = render_transcript_pgn(
            session.certificate,
            session.board,
            result=result,
            termination=termination,
        )
        session.result = GameResult(
            network=self.params.network_id,
            match_id=session.matchup.match_id,
            game_hash=canonical_game_hash(self.params.network_id, session.san_moves, result),
            result=cast(ResultValue, result.value),
            termination=cast(TerminationValue, termination.value),
            plies=len(session.san_moves),
        )
        session.status = SessionStatus.FINISHED

    # -- rounds and matches --------------------------------------------------

    @property
    def current_round(self) -> int | None:
        """The highest round opened for the current epoch, if any."""
        with self._lock:
            return self._current_round_locked()

    def _current_round_locked(self) -> int | None:
        rounds = list(self._rounds_opened) or [
            session.matchup.round
            for session in self._sessions.values()
            if session.matchup.epoch == self.beacon.epoch
        ]
        return max(rounds) if rounds else None

    def open_round(self, round_number: int) -> list[MatchView]:
        """Derive the pairings for ``round_number`` and create their sessions."""
        with self._lock:
            now_ms = self._now_ms()
            if self._sweep_locked(now_ms):
                self._persist_locked()
            for session in self._sessions.values():
                if (
                    session.matchup.epoch == self.beacon.epoch
                    and session.matchup.round == round_number
                ):
                    raise RelayError(
                        f"round {round_number} of epoch {self.beacon.epoch} is already open"
                    )
            beacon_bytes = bytes.fromhex(self.beacon.hash)
            matchups = pair(
                list(self._roster.values()),
                beacon_bytes,
                network=self.params.network_id,
                epoch=self.beacon.epoch,
                round_number=round_number,
                rounds_per_epoch=self.params.rounds_per_epoch,
                opponent_cooldown=self.params.opponent_cooldown,
            )
            now = self._now()
            created: list[MatchSession] = []
            for matchup in matchups:
                opening = self.book.select(beacon_bytes, matchup.match_id)
                certificate = create_certificate(
                    matchup,
                    opening,
                    engine_white=self._engines[matchup.white],
                    engine_black=self._engines[matchup.black],
                )
                board = chess.Board()
                san_moves: list[str] = []
                for san in opening.moves:
                    move = board.parse_san(san)
                    san_moves.append(board.san(move))
                    board.push(move)
                session = MatchSession(
                    matchup=matchup,
                    certificate=certificate,
                    board=board,
                    san_moves=san_moves,
                    status=SessionStatus.PENDING,
                    created_at=now,
                    last_seen_at=now,
                )
                self._sessions[matchup.match_id] = session
                created.append(session)
            if created:
                self._rounds_opened[round_number] = now
                self._persist_locked()
            return [session.view(now_ms) for session in created]

    def maybe_open_next_round(self, interval_seconds: float) -> list[MatchView] | None:
        """Open the next round once ``interval_seconds`` have passed.

        This is the relay's timed-session schedule: round 0 opens when the relay
        starts, and every ``interval_seconds`` after a round opens, the relay
        derives the next one. The window is a relay policy, not consensus — the
        beacon, the roster and the round number are all that a verifier needs.
        """
        if interval_seconds <= 0:
            return None
        with self._lock:
            now = self._now()
            current = self._current_round_locked()
            if current is None:
                return self.open_round(0)
            opened_at = self._rounds_opened.get(current, now)
            if now - opened_at < interval_seconds:
                return None
            if current + 1 >= self.params.rounds_per_epoch:
                return None
            return self.open_round(current + 1)

    def schedule(self, *, round_seconds: float) -> list[MatchView] | None:
        """Alias for :meth:`maybe_open_next_round` used by the relay service."""
        return self.maybe_open_next_round(round_seconds)

    def matches(self) -> list[MatchView]:
        """Every session, ordered by creation time and match id."""
        with self._lock:
            now_ms = self._now_ms()
            if self._sweep_locked(now_ms):
                self._persist_locked()
            ordered = sorted(
                self._sessions.values(),
                key=lambda item: (item.created_at, item.matchup.match_id),
            )
            return [session.view(now_ms) for session in ordered]

    def get(self, match_id: str) -> MatchView:
        """Return one session's public state."""
        with self._lock:
            now_ms = self._now_ms()
            if self._sweep_locked(now_ms):
                self._persist_locked()
            return self._require_session(match_id).view(now_ms)

    def clock_bundle(self, match_id: str) -> ClockBundle:
        """The signed receipts and flag for one match (the claim's evidence)."""
        with self._lock:
            now_ms = self._now_ms()
            if self._sweep_locked(now_ms):
                self._persist_locked()
            session = self._require_session(match_id)
            clocks = session.snapshot(now_ms)
            return ClockBundle(
                match_id=session.matchup.match_id,
                time_control=session.certificate.time_control,
                turn=session.turn,
                white_ms=clocks["white"],
                black_ms=clocks["black"],
                last_ply=len(session.san_moves),
                started=session.clock is not None,
                receipts=tuple(session.receipts),
                flag=session.flag,
            )

    def transcript(self, match_id: str) -> Transcript:
        """Return a co-signed transcript, or raise if the match is not complete."""
        with self._lock:
            session = self._require_session(match_id)
            if (
                session.status is not SessionStatus.FINISHED
                or session.result is None
                or session.pgn is None
            ):
                raise RelayError(f"match {match_id} has no finished transcript")
            if len(session.result.result_signatures) != 2:
                raise RelayError(f"match {match_id} is not co-signed yet")
            return Transcript(
                certificate=session.certificate,
                result=session.result,
                pgn=session.pgn,
            )

    def join(self, match_id: str, request: JoinRequest) -> MatchView:
        """Record a seat's certificate signature; activate when both have joined.

        Activation starts both clocks: the "second start signature
        activates the session" rule (PROTOCOL.md section 8.4).
        """
        with self._lock:
            now_ms = self._now_ms()
            if self._sweep_locked(now_ms):
                self._persist_locked()
            session = self._require_session(match_id)
            if session.status is not SessionStatus.PENDING:
                raise RelayError(
                    f"match {match_id} is {session.status.value}, not open for joining"
                )
            seat = _seat_colour(session, request.seat)
            registered = self._public_keys[request.seat]
            if request.public_key != registered:
                raise RelayError(f"public key does not match the registered key for {request.seat}")
            try:
                raw_key = bytes.fromhex(request.public_key)
                key = public_key_from_hex(request.public_key)
            except (ValueError, KeyEncodingError):
                raise RelayError("public key is not valid hex") from None
            if encode_address(raw_key, self.params) != request.seat:
                raise RelayError(f"public key derives a different address than {request.seat}")
            if seat in session.certificate.start_signatures:
                raise RelayError(f"{request.seat} has already joined match {match_id}")
            if not verify_domain(
                key,
                Domain.CERTIFICATE,
                certificate_payload(session.certificate),
                bytes.fromhex(request.signature),
            ):
                raise RelayError(f"invalid start signature from {request.seat}")
            signatures = dict(session.certificate.start_signatures)
            signatures[seat] = request.signature
            session.certificate = session.certificate.model_copy(
                update={"start_signatures": signatures}
            )
            session.last_seen_at = self._now()
            if set(signatures) == {"white", "black"}:
                session.status = SessionStatus.ACTIVE
                self._start_clock_locked(session, now_ms)
            self._persist_locked()
            return session.view(now_ms)

    def _signed_move_locked(self, session: MatchSession, request: MoveRequest) -> None:
        """Check the seat-signed part of a clocked move body."""
        if request.network is not None and request.network != self.params.network_id:
            raise RelayError(
                f"move is for {request.network!r}, this relay is on {self.params.network_id!r}"
            )
        if request.ply is None:
            raise RelayError("a signed move must carry its ply")
        expected_ply = len(session.san_moves)
        if request.ply != expected_ply:
            raise RelayError(
                f"move claims ply {request.ply}, but the next move is ply {expected_ply}"
            )
        assert request.signature is not None
        message = MoveMessage(
            network=self.params.network_id,
            match_id=session.matchup.match_id,
            seat=request.seat,
            ply=request.ply,
            san=request.san,
            signature=request.signature,
            clock_ms=request.clock_ms or 0,
        )
        try:
            verify_move_signature(
                message,
                certificate=session.certificate,
                public_keys=self._public_keys,
            )
        except ClockError as exc:
            raise RelayError(str(exc)) from None

    def play(self, match_id: str, request: MoveRequest) -> MatchView:
        """Validate and apply one SAN move, charging the mover's clock.

        The move is checked for legality *before* the clock is charged, so an
        illegal SAN never costs time. A move that arrives after the mover's
        clock reached zero is refused and the session is flagged instead: the
        flag ends the game whether or not the losing seat ever answers.
        """
        with self._lock:
            now_ms = self._now_ms()
            if self._sweep_locked(now_ms):
                self._persist_locked()
            session = self._require_session(match_id)
            if session.status is not SessionStatus.ACTIVE:
                raise RelayError(f"match {match_id} is {session.status.value}, not accepting moves")
            colour = _seat_colour(session, request.seat)
            turn = "white" if session.board.turn == chess.WHITE else "black"
            if colour != turn:
                raise RelayError(f"it is {turn}'s turn, not {colour}'s")
            if session.board.ply() >= self.params.max_plies:
                raise RelayError(f"match {match_id} reached max_plies")
            try:
                move = session.board.parse_san(request.san)
            except ValueError:
                raise RelayError(f"{request.san!r} is not a legal move in this position") from None
            san = session.board.san(move)
            if request.signed:
                if request.san != san:
                    raise RelayError(
                        f"{request.san!r} is not the canonical SAN for this move ({san!r})"
                    )
                self._signed_move_locked(session, request)
            if session.clock is None:
                self._start_clock_locked(session, now_ms)
            clock = session.clock
            assert clock is not None
            if clock.is_flagged(now_ms):
                self._flag_locked(session, now_ms)
                self._persist_locked()
                raise RelayError(f"match {match_id} ended on time: {colour} has no time left")
            try:
                clock.apply_move(now_ms)
            except ClockError:
                self._flag_locked(session, now_ms)
                self._persist_locked()
                raise RelayError(f"{colour} ran out of time in match {match_id}") from None
            session.board.push(move)
            session.san_moves.append(san)
            session.last_seen_at = self._now()
            clocks = clock.snapshot(now_ms)
            receipt = MoveReceipt(
                match_id=session.matchup.match_id,
                ply=len(session.san_moves) - 1,
                seat=request.seat,
                san=san,
                received_at_ms=now_ms,
                white_ms=clocks["white"],
                black_ms=clocks["black"],
            )
            signed_receipt = self._sign_receipt_locked(receipt)
            assert isinstance(signed_receipt, MoveReceipt)
            session.receipts.append(signed_receipt)
            outcome = session.board.outcome(claim_draw=False)
            if outcome is not None:
                result, termination = outcome_result(outcome)
                self._finish_locked(session, result, termination, now_ms=now_ms)
            self._persist_locked()
            return session.view(now_ms)

    def resign(self, match_id: str, request: ResignRequest) -> MatchView:
        """Finish a match because the seat to move resigns."""
        with self._lock:
            now_ms = self._now_ms()
            if self._sweep_locked(now_ms):
                self._persist_locked()
            session = self._require_session(match_id)
            if session.status is not SessionStatus.ACTIVE:
                raise RelayError(f"match {match_id} is {session.status.value}, cannot resign")
            colour = _seat_colour(session, request.seat)
            turn = "white" if session.board.turn == chess.WHITE else "black"
            if colour != turn:
                raise RelayError(f"only the side to move ({turn}) may resign")
            result = PgnResult.BLACK if session.board.turn == chess.WHITE else PgnResult.WHITE
            self._finish_locked(session, result, Termination.RESIGNATION, now_ms=now_ms)
            session.last_seen_at = self._now()
            self._persist_locked()
            return session.view(now_ms)

    def sign_result(self, match_id: str, request: ResultRequest) -> MatchView:
        """Record one seat's result signature."""
        with self._lock:
            session = self._require_session(match_id)
            if (
                session.status is not SessionStatus.FINISHED
                or session.result is None
                or session.pgn is None
            ):
                raise RelayError(f"match {match_id} has no result to sign")
            seat = _seat_colour(session, request.seat)
            if seat in session.result.result_signatures:
                raise RelayError(f"{request.seat} has already signed match {match_id}")
            key = public_key_from_hex(self._public_keys[request.seat])
            if not verify_domain(
                key,
                Domain.RESULT,
                result_payload(session.result),
                bytes.fromhex(request.signature),
            ):
                raise RelayError(f"invalid result signature from {request.seat}")
            signatures = dict(session.result.result_signatures)
            signatures[seat] = request.signature
            session.result = session.result.model_copy(update={"result_signatures": signatures})
            session.last_seen_at = self._now()
            self._persist_locked()
            return session.view(self._now_ms())

    def expire(self) -> list[str]:
        """Abandon joins that have been silent past the timeout."""
        with self._lock:
            expired = self._expire_locked()
            if expired:
                self._persist_locked()
            return expired

    def sweep(self) -> list[str]:
        """Flag expired clocks and reap silent joins; return finished match ids.

        The relay's service loop calls this on a timer, and every read path
        calls it too, so a flag is observed even when nobody is polling.
        """
        with self._lock:
            now_ms = self._now_ms()
            before = {
                match_id
                for match_id, session in self._sessions.items()
                if session.status is SessionStatus.FINISHED
            }
            if self._sweep_locked(now_ms):
                self._persist_locked()
            return sorted(
                match_id
                for match_id, session in self._sessions.items()
                if session.status is SessionStatus.FINISHED and match_id not in before
            )

    def reputation(self) -> dict[str, int]:
        """No-show counts per address (relay-local, never consensus)."""
        with self._lock:
            return dict(sorted(self._no_shows.items()))

    @classmethod
    def from_document(
        cls,
        document: Mapping[str, Any],
        *,
        params: NetworkParams | None = None,
        store: Path | None = None,
        book: OpeningBook | None = None,
        witness_keypair: Keypair | None = None,
        clock: Callable[[], int] | None = None,
        clock_ms: Callable[[], int] | None = None,
        lag_grace_ms: int = DEFAULT_LAG_GRACE_MS,
    ) -> Matchmaker:
        """Build a relay from the JSON config used by ``sccp matchmaker run``.

        The config carries the epoch beacon, the roster with public keys and
        engine fingerprints, and optionally ``timeout_seconds``,
        ``round_seconds`` and an ``open_round`` number to open immediately. The
        witness keypair is not in the config: the CLI loads it from a keyfile
        and passes it in, so a relay store never contains key material.
        """
        if not isinstance(document, Mapping):
            raise RelayError("relay config must be a JSON object")
        network = document.get("network")
        if not isinstance(network, str) or not network:
            raise RelayError("relay config needs a network id")
        if params is None:
            params = get_network(network)
        elif params.network_id != network:
            raise RelayError(
                f"config is for {network!r}, but --network selected {params.network_id!r}"
            )
        beacon_document = document.get("beacon")
        if not isinstance(beacon_document, Mapping):
            raise RelayError("relay config needs a beacon object")
        try:
            beacon = Beacon.model_validate(beacon_document)
        except ValueError as exc:
            raise RelayError(f"relay beacon is invalid: {exc}") from None
        roster_document = document.get("roster")
        if not isinstance(roster_document, Mapping) or not roster_document:
            raise RelayError("relay config needs a non-empty roster object")
        roster: dict[str, RosterEntry] = {}
        public_keys: dict[str, str] = {}
        engines: dict[str, str] = {}
        for address, member_document in roster_document.items():
            if not isinstance(address, str) or not isinstance(member_document, Mapping):
                raise RelayError("relay roster entries must map an address to an object")
            try:
                member = RosterMember.model_validate(member_document)
            except ValueError as exc:
                raise RelayError(f"relay roster entry {address} is invalid: {exc}") from None
            if member.engine is None:
                raise RelayError(f"relay roster entry {address} needs an engine fingerprint")
            roster[address] = RosterEntry(
                address=address,
                max_games=member.max_games,
                funder=member.funder,
                subnet=member.subnet,
                asn=member.asn,
                recent_opponents=member.recent_opponents,
            )
            public_keys[address] = member.public_key
            engines[address] = member.engine
        timeout = document.get("timeout_seconds", 300)
        if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout < 1:
            raise RelayError("timeout_seconds must be a positive integer")
        round_seconds = document.get("round_seconds", 0)
        if (
            not isinstance(round_seconds, (int, float))
            or isinstance(round_seconds, bool)
            or round_seconds < 0
        ):
            raise RelayError("round_seconds must be a non-negative number")
        matchmaker = cls(
            params,
            beacon=beacon,
            roster=roster,
            public_keys=public_keys,
            engines=engines,
            book=book_for(params) if book is None else book,
            store=store,
            clock=clock,
            clock_ms=clock_ms,
            timeout_seconds=timeout,
            witness_keypair=witness_keypair,
            lag_grace_ms=lag_grace_ms,
        )
        matchmaker.round_seconds = float(round_seconds)
        open_round = document.get("open_round")
        if open_round is not None:
            if not isinstance(open_round, int) or isinstance(open_round, bool) or open_round < 0:
                raise RelayError("open_round must be a non-negative integer")
            matchmaker.open_round(open_round)
        elif matchmaker.round_seconds:
            matchmaker.open_round(0)
        return matchmaker


def create_app(matchmaker: Matchmaker) -> FastAPI:
    """Wrap a :class:`Matchmaker` in the relay's HTTP API."""
    app = FastAPI(title="sccp matchmaker", version="1")

    def call(action: Callable[[], T]) -> T:
        try:
            return action()
        except RelayError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None

    @app.get("/v1/status")
    def relay_status() -> dict[str, object]:
        views = matchmaker.matches()
        return {
            "network": matchmaker.params.network_id,
            "epoch": matchmaker.beacon.epoch,
            "beacon": matchmaker.beacon.model_dump(mode="json"),
            "matches": len(views),
            "open": sum(view.status in ("pending", "active") for view in views),
            "reputation": matchmaker.reputation(),
            "timed": TIMED_SESSIONS,
            "time_control": FIXED_TIME_CONTROL.model_dump(mode="json"),
            "round": matchmaker.current_round,
            "round_seconds": matchmaker.round_seconds,
            "witness": matchmaker.witness,
        }

    @app.get("/v1/matches", response_model=list[MatchView])
    def list_matches() -> list[MatchView]:
        return matchmaker.matches()

    @app.get("/v1/matches/{match_id}", response_model=MatchView)
    def get_match(match_id: str) -> MatchView:
        try:
            return matchmaker.get(match_id)
        except RelayError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None

    @app.get("/v1/matches/{match_id}/receipts", response_model=ClockBundle)
    def get_receipts(match_id: str) -> ClockBundle:
        try:
            return matchmaker.clock_bundle(match_id)
        except RelayError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None

    @app.get("/v1/matches/{match_id}/transcript", response_model=Transcript)
    def get_transcript(match_id: str) -> Transcript:
        try:
            return matchmaker.transcript(match_id)
        except RelayError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None

    @app.post("/v1/rounds", response_model=list[MatchView])
    def open_round(request: RoundRequest) -> list[MatchView]:
        return call(lambda: matchmaker.open_round(request.round))

    @app.post("/v1/matches/{match_id}/join", response_model=MatchView)
    def join(match_id: str, request: JoinRequest) -> MatchView:
        return call(lambda: matchmaker.join(match_id, request))

    @app.post("/v1/matches/{match_id}/move", response_model=MatchView)
    def move(match_id: str, request: MoveRequest) -> MatchView:
        return call(lambda: matchmaker.play(match_id, request))

    @app.post("/v1/matches/{match_id}/resign", response_model=MatchView)
    def resign(match_id: str, request: ResignRequest) -> MatchView:
        return call(lambda: matchmaker.resign(match_id, request))

    @app.post("/v1/matches/{match_id}/result", response_model=MatchView)
    def result(match_id: str, request: ResultRequest) -> MatchView:
        return call(lambda: matchmaker.sign_result(match_id, request))

    @app.get("/v1/reputation")
    def reputation() -> dict[str, int]:
        return matchmaker.reputation()

    return app


__all__ = [
    "STORE_FORMAT",
    "TIMED_SESSIONS",
    "ClockBundle",
    "JoinRequest",
    "MatchSession",
    "MatchView",
    "Matchmaker",
    "MoveRequest",
    "ResignRequest",
    "ResultRequest",
    "RoundRequest",
    "SessionStatus",
    "create_app",
    "relay_monotonic_ms",
]
