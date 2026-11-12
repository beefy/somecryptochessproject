"""The matchmaking relay (FastAPI), section 5.7.

The relay is a convenience service, not a trusted party. It derives pairings
from the beacon, opens a session per matchup, relays SAN moves after checking
them, and stores both seats' signatures. Every object it returns can be
recomputed and checked by anyone with the roster and the beacon.

State lives in :class:`Matchmaker` and is protected by a re-entrant lock; the
HTTP layer is a thin translation of the same calls. A JSON store (optional)
makes restart safe: sessions are serialised with their SAN list and replayed on
load, so pending matches keep their position and finished transcripts survive.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
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
from sccp.crypto.keys import public_key_from_hex, verify_domain
from sccp.errors import KeyEncodingError, RelayError
from sccp.protocol.models import (
    AddressText,
    Beacon,
    Color,
    GameResult,
    MatchCertificate,
    Matchup,
    ProtocolModel,
    PublicKeyHex,
    ResultValue,
    RosterMember,
    SignatureHex,
    TerminationValue,
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

STORE_FORMAT = 1

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
    """A seat submits one SAN move."""

    seat: AddressText
    san: str = Field(min_length=1, max_length=16)


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
    """The public state of a session."""

    matchup: Matchup
    status: str
    san_moves: tuple[str, ...]
    certificate: MatchCertificate
    result: GameResult | None = None
    pgn: str | None = None


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

    def view(self) -> MatchView:
        return MatchView(
            matchup=self.matchup,
            status=self.status.value,
            san_moves=tuple(self.san_moves),
            certificate=self.certificate,
            result=self.result,
            pgn=self.pgn,
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
            if result.termination != "resignation":
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
            elif board.outcome(claim_draw=False) is not None:
                raise RelayError(
                    f"stored match {matchup.match_id} claims resignation on a finished board"
                )
        elif result is not None:
            raise RelayError(f"stored match {matchup.match_id} is not finished but has a result")
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
        timeout_seconds: int = 300,
    ) -> None:
        if beacon.network != params.network_id:
            raise RelayError(f"beacon is for {beacon.network!r}, relay is on {params.network_id!r}")
        if timeout_seconds < 1:
            raise RelayError("timeout_seconds must be at least 1")

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
        self.timeout_seconds = timeout_seconds
        self._sessions: dict[str, MatchSession] = {}
        self._no_shows: dict[str, int] = {}
        self._lock = threading.RLock()
        self._load()

    # -- persistence ---------------------------------------------------------

    def _load(self) -> None:
        if self._store is None or not self._store.exists():
            return
        try:
            document = json.loads(self._store.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RelayError(f"cannot read relay store {self._store}: {exc}") from None
        if not isinstance(document, dict) or document.get("format") != STORE_FORMAT:
            raise RelayError(f"relay store {self._store} has an unsupported format")
        if document.get("network") != self.params.network_id:
            raise RelayError(
                f"relay store {self._store} belongs to network {document.get('network')!r}"
            )
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
        now = self._now()
        expired: list[str] = []
        for session in self._sessions.values():
            if session.status not in (SessionStatus.PENDING, SessionStatus.ACTIVE):
                continue
            if now - session.last_seen_at <= self.timeout_seconds:
                continue
            previous = session.status
            session.status = SessionStatus.ABANDONED
            expired.append(session.matchup.match_id)
            if previous is SessionStatus.PENDING:
                missing = [
                    seat
                    for seat in (session.certificate.white, session.certificate.black)
                    if seat not in session.certificate.start_signatures
                ]
            else:
                missing = [session.certificate.white, session.certificate.black]
            for seat in missing:
                self._no_shows[seat] = self._no_shows.get(seat, 0) + 1
        return expired

    def _finish_locked(
        self,
        session: MatchSession,
        result: PgnResult,
        termination: Termination,
    ) -> None:
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

    def open_round(self, round_number: int) -> list[MatchView]:
        """Derive the pairings for ``round_number`` and create their sessions."""
        with self._lock:
            self._expire_locked()
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
                self._persist_locked()
            return [session.view() for session in created]

    def matches(self) -> list[MatchView]:
        """Every session, ordered by creation time and match id."""
        with self._lock:
            if self._expire_locked():
                self._persist_locked()
            ordered = sorted(
                self._sessions.values(),
                key=lambda item: (item.created_at, item.matchup.match_id),
            )
            return [session.view() for session in ordered]

    def get(self, match_id: str) -> MatchView:
        """Return one session's public state."""
        with self._lock:
            if self._expire_locked():
                self._persist_locked()
            return self._require_session(match_id).view()

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
        """Record a seat's certificate signature; activate when both have joined."""
        with self._lock:
            self._expire_locked()
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
            self._persist_locked()
            return session.view()

    def play(self, match_id: str, request: MoveRequest) -> MatchView:
        """Validate and apply one SAN move."""
        with self._lock:
            self._expire_locked()
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
            session.board.push(move)
            session.san_moves.append(san)
            session.last_seen_at = self._now()
            outcome = session.board.outcome(claim_draw=False)
            if outcome is not None:
                result, termination = outcome_result(outcome)
                self._finish_locked(session, result, termination)
            self._persist_locked()
            return session.view()

    def resign(self, match_id: str, request: ResignRequest) -> MatchView:
        """Finish a match because the seat to move resigns."""
        with self._lock:
            self._expire_locked()
            session = self._require_session(match_id)
            if session.status is not SessionStatus.ACTIVE:
                raise RelayError(f"match {match_id} is {session.status.value}, cannot resign")
            colour = _seat_colour(session, request.seat)
            turn = "white" if session.board.turn == chess.WHITE else "black"
            if colour != turn:
                raise RelayError(f"only the side to move ({turn}) may resign")
            result = PgnResult.BLACK if session.board.turn == chess.WHITE else PgnResult.WHITE
            self._finish_locked(session, result, Termination.RESIGNATION)
            session.last_seen_at = self._now()
            self._persist_locked()
            return session.view()

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
            return session.view()

    def expire(self) -> list[str]:
        """Abandon sessions that have been silent past the timeout."""
        with self._lock:
            expired = self._expire_locked()
            if expired:
                self._persist_locked()
            return expired

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
    ) -> Matchmaker:
        """Build a relay from the JSON config used by ``sccp matchmaker run``.

        The config carries the epoch beacon, the roster with public keys and
        engine fingerprints, and optionally ``timeout_seconds`` and an
        ``open_round`` number to open immediately.
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
        matchmaker = cls(
            params,
            beacon=beacon,
            roster=roster,
            public_keys=public_keys,
            engines=engines,
            book=book_for(params) if book is None else book,
            store=store,
            timeout_seconds=timeout,
        )
        open_round = document.get("open_round")
        if open_round is not None:
            if not isinstance(open_round, int) or isinstance(open_round, bool) or open_round < 0:
                raise RelayError("open_round must be a non-negative integer")
            matchmaker.open_round(open_round)
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
]
