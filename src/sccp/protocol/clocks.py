"""Match clocks, signed moves and clock evidence (PROTOCOL.md section 8.4).

The protocol has exactly one time control: 30+0. A match starts with two clocks
at :data:`FIXED_TIME_CONTROL.initial_ms`; only the side to move spends time. A
move deducts ``max(0, elapsed - lag_grace_ms)`` from the mover and rejects the
move when the result would be ``<= 0``. The relay is authoritative for live
play; the on-chain verifier only sees the signed move stream and the witness
receipts in :class:`~sccp.protocol.models.ClockEvidence`.

Nothing here trusts a relay timestamp: move signatures cover the protocol move
payload, and witness signatures cover the receipt fields. Clock arithmetic is
deterministic integer arithmetic, so the same evidence validates identically on
every platform.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

import chess

from sccp.crypto.address import encode_address
from sccp.crypto.keys import (
    Keypair,
    PublicKeyLike,
    public_key_from_hex,
    sign_domain,
    verify_domain,
)
from sccp.errors import ClockError
from sccp.protocol.models import (
    MAX_CLOCK_MS,
    MAX_PLIES_LIMIT,
    ClockEvidence,
    ClockWitnessSignature,
    Color,
    FlagReceipt,
    MatchCertificate,
    MoveMessage,
    MoveReceipt,
    ResultValue,
    SignedMove,
    TimeControl,
    TimeoutClaim,
)
from sccp.serialization import Domain

FIXED_TIME_CONTROL: Final = TimeControl(initial_ms=1_800_000, increment_ms=0)
"""The only legal v1 control: 30 minutes per seat, no increment."""

DEFAULT_LAG_GRACE_MS: Final = 250
"""Network-jitter credit granted once per move (PROTOCOL.md section 8.4)."""

MAX_LAG_GRACE_MS: Final = 1_000
"""Largest relay-policy grace; it never enters consensus or the game hash."""

OPPOSITE: Final[dict[Color, Color]] = {"white": "black", "black": "white"}

_TIME_CONTROL_TAG_PATTERN: Final = re.compile(r"(?P<initial>[0-9]+)\+(?P<increment>[0-9]+)\Z")


def opposite(seat: Color) -> Color:
    """Return the other colour."""
    return OPPOSITE[seat]


def validate_time_control(
    control: TimeControl,
    *,
    max_plies: int,
    require_fixed: bool = False,
) -> None:
    """Check the per-network clock bounds and, optionally, the fixed v1 control."""
    if require_fixed and control != FIXED_TIME_CONTROL:
        raise ClockError(
            f"time control {control.model_dump()} is not the protocol-fixed "
            f"{FIXED_TIME_CONTROL.model_dump()}"
        )
    upper = control.initial_ms + control.increment_ms * max_plies
    if upper > MAX_CLOCK_MS:
        raise ClockError(
            f"time control exceeds {MAX_CLOCK_MS} ms for max_plies={max_plies}: "
            f"{control.initial_ms} + {control.increment_ms} * {max_plies} = {upper}"
        )


def time_control_tag(control: TimeControl) -> str:
    """Render a control as a lossless PGN ``TimeControl`` tag (``"1800+0"``)."""
    return f"{control.initial_ms // 1000}+{control.increment_ms // 1000}"


def time_control_from_tag(tag: str) -> TimeControl:
    """Parse a PGN ``TimeControl`` tag, rejecting fractional seconds and junk."""
    match = _TIME_CONTROL_TAG_PATTERN.fullmatch(tag)
    if match is None:
        raise ClockError(f"invalid TimeControl tag: {tag!r}")
    initial_seconds = int(match.group("initial"))
    increment_seconds = int(match.group("increment"))
    if initial_seconds < 1:
        raise ClockError(f"invalid TimeControl tag: {tag!r}")
    worst_case = initial_seconds * 1000 + increment_seconds * 1000 * MAX_PLIES_LIMIT
    if worst_case > MAX_CLOCK_MS:
        raise ClockError(
            f"TimeControl tag {tag!r} exceeds MAX_CLOCK_MS for max_plies={MAX_PLIES_LIMIT}"
        )
    return TimeControl(
        initial_ms=initial_seconds * 1000,
        increment_ms=increment_seconds * 1000,
    )


def _check_grace(lag_grace_ms: int) -> int:
    if not 0 <= lag_grace_ms <= MAX_LAG_GRACE_MS:
        raise ClockError(f"lag_grace_ms must be in [0, {MAX_LAG_GRACE_MS}]")
    return lag_grace_ms


def _check_registered_key(
    public_keys: Mapping[str, PublicKeyLike | str], address: str
) -> PublicKeyLike:
    key = public_keys.get(address)
    if key is None:
        raise ClockError(f"no registered public key for {address}")
    return public_key_from_hex(key) if isinstance(key, str) else key


@dataclass(slots=True)
class MatchClock:
    """A mutable two-clock state for one live session.

    ``turn_started_at_ms`` comes from the relay's monotonic clock. Negative
    elapsed time is clamped to zero, so a clock source that steps backwards
    cannot create time.
    """

    control: TimeControl
    white_ms: int
    black_ms: int
    turn: Color
    turn_started_at_ms: int
    lag_grace_ms: int = DEFAULT_LAG_GRACE_MS
    started: bool = True

    @classmethod
    def start(
        cls,
        control: TimeControl = FIXED_TIME_CONTROL,
        *,
        now_ms: int,
        lag_grace_ms: int = DEFAULT_LAG_GRACE_MS,
    ) -> MatchClock:
        """Start both clocks at ``control.initial_ms`` with White to move."""
        _check_grace(lag_grace_ms)
        if now_ms < 0:
            raise ClockError("now_ms must not be negative")
        return cls(
            control=control,
            white_ms=control.initial_ms,
            black_ms=control.initial_ms,
            turn="white",
            turn_started_at_ms=now_ms,
            lag_grace_ms=lag_grace_ms,
        )

    def elapsed_ms(self, now_ms: int) -> int:
        """Whole milliseconds since the current turn began, never negative."""
        if now_ms < 0:
            raise ClockError("now_ms must not be negative")
        return max(0, now_ms - self.turn_started_at_ms)

    def remaining_ms(self, now_ms: int) -> dict[Color, int]:
        """The remaining clocks after applying elapsed time and lag grace.

        A clock that is not running (never started, or stopped after the game
        ended) does not count down: its stored values are returned unchanged.
        """
        if not self.started:
            return {"white": self.white_ms, "black": self.black_ms}
        deduction = max(0, self.elapsed_ms(now_ms) - self.lag_grace_ms)
        current = self.white_ms if self.turn == "white" else self.black_ms
        remaining = max(0, current - deduction)
        if self.turn == "white":
            return {"white": remaining, "black": self.black_ms}
        return {"white": self.white_ms, "black": remaining}

    def current_remaining_ms(self, now_ms: int) -> int:
        """Remaining time for the side to move."""
        return self.remaining_ms(now_ms)[self.turn]

    def is_flagged(self, now_ms: int) -> bool:
        """True when the side to move has no time left before making a move."""
        return self.current_remaining_ms(now_ms) <= 0

    def apply_move(self, now_ms: int) -> int:
        """Deduct the mover's elapsed time, add increment and switch turns.

        Returns the mover's remaining time after the move. Raises
        :class:`~sccp.errors.ClockError` when the move arrives at or after zero.
        """
        if not self.started:
            raise ClockError("the match clock has not started")
        started_at = max(self.turn_started_at_ms, now_ms)
        deduction = max(0, self.elapsed_ms(now_ms) - self.lag_grace_ms)
        current = self.white_ms if self.turn == "white" else self.black_ms
        after = current - deduction
        if after <= 0:
            raise ClockError(f"{self.turn} has no time left: {current} ms - {deduction} ms <= 0")
        after += self.control.increment_ms
        if self.turn == "white":
            self.white_ms = after
        else:
            self.black_ms = after
        self.turn = opposite(self.turn)
        # Never move the turn boundary backwards: a clock source that steps back
        # must not give the next mover more elapsed time than the previous one.
        self.turn_started_at_ms = started_at
        return after

    def flag(self, now_ms: int) -> Color | None:
        """Return the flagged colour, or ``None`` when both clocks are positive."""
        return self.turn if self.is_flagged(now_ms) else None

    def stop(self, now_ms: int) -> None:
        """Freeze the clocks once the game is over.

        The stored values become the snapshot at ``now_ms`` and ``started`` is
        cleared, so later reads (a finished match view) are stable instead of
        counting down a game that is no longer being played.
        """
        frozen = self.remaining_ms(now_ms)
        self.white_ms = frozen["white"]
        self.black_ms = frozen["black"]
        self.started = False

    def snapshot(self, now_ms: int) -> dict[Color, int]:
        """The relay-visible ``{white_ms, black_ms}`` pair."""
        return self.remaining_ms(now_ms)


def flag_result(board: chess.Board, *, flagged: Color) -> ResultValue:
    """FIDE flag-fall result: a win unless the winner cannot mate by any line."""
    winner = opposite(flagged)
    if board.is_insufficient_material():
        return "1/2-1/2"
    return "1-0" if winner == "white" else "0-1"


def move_payload(move: SignedMove | MoveMessage) -> dict[str, object]:
    """The exact payload a seat signs for one move."""
    return {
        "network": move.network,
        "match_id": move.match_id,
        "seat": move.seat,
        "ply": move.ply,
        "san": move.san,
    }


def sign_move(move: SignedMove | MoveMessage, keypair: Keypair) -> MoveMessage:
    """Sign ``move`` and return the same object with its signature filled in.

    ``clock_ms`` on a :class:`MoveMessage` is display data and is not covered by
    the signature; a plain :class:`SignedMove` is returned as a ``MoveMessage``
    with ``clock_ms = 0``.
    """
    if isinstance(move, MoveMessage):
        updated = move
    else:
        updated = MoveMessage(**move.model_dump(), clock_ms=0)
    signature = sign_domain(keypair, Domain.MOVE, move_payload(updated)).hex()
    return updated.model_copy(update={"signature": signature})


def verify_move_signature(
    move: SignedMove | MoveMessage,
    *,
    certificate: MatchCertificate,
    public_keys: Mapping[str, PublicKeyLike | str],
) -> None:
    """Verify a move's seat, network, match id and Ed25519 signature."""
    if move.network != certificate.network:
        raise ClockError(
            f"move is for {move.network!r}, certificate is for {certificate.network!r}"
        )
    if move.match_id != certificate.match_id:
        raise ClockError("move does not belong to this certificate")
    if move.seat not in (certificate.white, certificate.black):
        raise ClockError(f"{move.seat} is not a seat of {certificate.match_id}")
    expected: Color = "white" if move.ply % 2 == 0 else "black"
    expected_address = certificate.white if expected == "white" else certificate.black
    if move.seat != expected_address:
        raise ClockError(
            f"ply {move.ply} belongs to {expected} ({expected_address}), "
            f"but the move is signed by {move.seat}"
        )
    key = _check_registered_key(public_keys, move.seat)
    try:
        signature = bytes.fromhex(move.signature)
    except ValueError:
        raise ClockError("move signature is not valid hex") from None
    if not verify_domain(key, Domain.MOVE, move_payload(move), signature):
        raise ClockError(f"invalid {move.seat} move signature at ply {move.ply}")


def verify_move_evidence(
    evidence: Sequence[SignedMove] | None,
    *,
    certificate: MatchCertificate,
    public_keys: Mapping[str, PublicKeyLike | str],
    san_moves: Sequence[str],
) -> None:
    """Verify a refused-checkmate move stream against the replayed SAN list.

    Every post-opening move must be present, in order, signed by the seat whose
    turn it was. ``ply`` is the zero-based index into ``san_moves`` (PROTOCOL.md
    section 9).
    """
    if not evidence:
        raise ClockError("move_evidence is empty")
    opening_plies = len(certificate.opening.moves)
    expected_plies = list(range(opening_plies, len(san_moves)))
    if [move.ply for move in evidence] != expected_plies:
        raise ClockError("move_evidence does not cover every post-opening move exactly once")
    for move in evidence:
        if move.san != san_moves[move.ply]:
            raise ClockError(
                f"move_evidence SAN at ply {move.ply} is {move.san!r}, "
                f"the PGN says {san_moves[move.ply]!r}"
            )
        verify_move_signature(move, certificate=certificate, public_keys=public_keys)


def clock_receipt_payload(network: str, receipt: MoveReceipt | FlagReceipt) -> dict[str, object]:
    """The exact payload a witness signs for a move or flag receipt."""
    payload: dict[str, object] = {
        "network": network,
        "kind": receipt.kind,
        "match_id": receipt.match_id,
        "ply": receipt.ply,
        "seat": receipt.seat,
        "received_at_ms": receipt.received_at_ms,
        "white_ms": receipt.white_ms,
        "black_ms": receipt.black_ms,
    }
    if isinstance(receipt, MoveReceipt):
        payload["san"] = receipt.san
    return payload


def sign_receipt(
    receipt: MoveReceipt | FlagReceipt,
    keypair: Keypair,
    *,
    network: str,
    witness: str | None = None,
) -> MoveReceipt | FlagReceipt:
    """Add this witness's signature to ``receipt``."""
    address = witness or encode_address(keypair.public_key_bytes, network)
    if any(item.witness == address for item in receipt.witnesses):
        raise ClockError(f"witness {address} already signed this receipt")
    signature = sign_domain(keypair, Domain.CLOCK, clock_receipt_payload(network, receipt)).hex()
    updated = (*receipt.witnesses, ClockWitnessSignature(witness=address, signature=signature))
    return receipt.model_copy(update={"witnesses": updated})


def verify_receipt_signatures(
    receipt: MoveReceipt | FlagReceipt,
    *,
    network: str,
    public_keys: Mapping[str, PublicKeyLike | str],
    quorum: int,
) -> frozenset[str]:
    """Verify the witnesses on one receipt and return their unique addresses."""
    if quorum < 1:
        raise ClockError("clock_witness_quorum must be at least one")
    seen: set[str] = set()
    payload = clock_receipt_payload(network, receipt)
    for witness_signature in receipt.witnesses:
        if witness_signature.witness in seen:
            raise ClockError(
                f"witness {witness_signature.witness} signed one receipt more than once"
            )
        seen.add(witness_signature.witness)
        key = _check_registered_key(public_keys, witness_signature.witness)
        try:
            signature = bytes.fromhex(witness_signature.signature)
        except ValueError:
            raise ClockError("clock witness signature is not valid hex") from None
        if not verify_domain(key, Domain.CLOCK, payload, signature):
            raise ClockError(f"invalid clock witness signature from {witness_signature.witness}")
    if len(seen) < quorum:
        raise ClockError(f"receipt has {len(seen)} witness signature(s), quorum is {quorum}")
    return frozenset(seen)


def verify_clock_evidence(
    evidence: ClockEvidence,
    *,
    certificate: MatchCertificate,
    san_moves: Sequence[str],
    result: ResultValue,
    winner_seat: Color,
    public_keys: Mapping[str, PublicKeyLike | str],
    quorum: int,
    max_plies: int,
    lag_grace_ms: int = DEFAULT_LAG_GRACE_MS,
    require_fixed: bool = True,
) -> None:
    """Verify a complete flag bundle against a replayed game.

    ``san_moves`` is the full SAN list including the certified opening prefix.
    Move receipts use zero-based SAN indexes; the final flag receipt uses the
    total board ply count, so ``final.ply == len(san_moves)``.
    """
    _check_grace(lag_grace_ms)
    validate_time_control(
        evidence.time_control,
        max_plies=max_plies,
        require_fixed=require_fixed,
    )
    if evidence.time_control != certificate.time_control:
        raise ClockError("clock_evidence time_control does not match the certificate")
    if require_fixed:
        validate_time_control(certificate.time_control, max_plies=max_plies, require_fixed=True)

    plies = len(san_moves)
    final = evidence.final
    if final.match_id != certificate.match_id:
        raise ClockError("flag receipt does not belong to this certificate")
    if plies < 1:
        raise ClockError("a flag at ply 0 is a no-show, not a win")
    if final.ply != plies:
        raise ClockError(f"flag receipt is for ply {final.ply}, but the game has {plies} plies")
    flagged = opposite(winner_seat)
    to_move: Color = "white" if plies % 2 == 0 else "black"
    to_move_address = certificate.white if to_move == "white" else certificate.black
    if final.seat != to_move_address:
        raise ClockError(
            f"flag receipt names {final.seat}, but the side to move at ply {plies} "
            f"is {to_move} ({to_move_address})"
        )
    expected_result = "1-0" if winner_seat == "white" else "0-1"
    if result != expected_result:
        raise ClockError(
            f"time forfeit result {result!r} does not agree with winner seat {winner_seat!r}"
        )
    flagged_ms = final.white_ms if flagged == "white" else final.black_ms
    winner_ms = final.white_ms if winner_seat == "white" else final.black_ms
    if flagged_ms != 0:
        raise ClockError("the flagged seat's clock is not zero")
    if winner_ms <= 0:
        raise ClockError("the winner's clock is not positive")
    verify_receipt_signatures(
        final,
        network=certificate.network,
        public_keys=public_keys,
        quorum=quorum,
    )

    opening_plies = len(certificate.opening.moves)
    expected_indexes = list(range(opening_plies, plies))
    if [receipt.ply for receipt in evidence.receipts] != expected_indexes:
        raise ClockError("clock receipts do not cover every post-opening move exactly once")

    control = evidence.time_control
    running: dict[Color, int] = {
        "white": control.initial_ms,
        "black": control.initial_ms,
    }
    previous_received: int | None = None
    for receipt in evidence.receipts:
        if receipt.match_id != certificate.match_id:
            raise ClockError("clock receipt does not belong to this certificate")
        if receipt.san != san_moves[receipt.ply]:
            raise ClockError(
                f"receipt SAN at ply {receipt.ply} is {receipt.san!r}, "
                f"the PGN says {san_moves[receipt.ply]!r}"
            )
        mover_seat: Color = "white" if receipt.ply % 2 == 0 else "black"
        mover_address = certificate.white if mover_seat == "white" else certificate.black
        if receipt.seat != mover_address:
            raise ClockError(
                f"ply {receipt.ply} belongs to {mover_seat} ({mover_address}), "
                f"receipt names {receipt.seat}"
            )
        if previous_received is not None and receipt.received_at_ms < previous_received:
            raise ClockError("clock receipt timestamps are not monotonic")
        verify_receipt_signatures(
            receipt,
            network=certificate.network,
            public_keys=public_keys,
            quorum=quorum,
        )
        white_after = receipt.white_ms
        black_after = receipt.black_ms
        actual_after = white_after if mover_seat == "white" else black_after
        if previous_received is None:
            # The first receipt has no verifiable turn-start timestamp, but the
            # idle clock must still be untouched and neither clock may grow
            # beyond one increment.
            ceiling = control.initial_ms + control.increment_ms
            if not 0 < white_after <= ceiling:
                raise ClockError("first receipt's white clock is out of range")
            if not 0 < black_after <= ceiling:
                raise ClockError("first receipt's black clock is out of range")
            idle: Color = opposite(mover_seat)
            actual_idle = white_after if idle == "white" else black_after
            if actual_idle != control.initial_ms:
                raise ClockError("receipt changed the clock of the idle side")
        else:
            elapsed = receipt.received_at_ms - previous_received
            deduction = max(0, elapsed - lag_grace_ms)
            expected_after = running[mover_seat] - deduction + control.increment_ms
            if expected_after <= 0:
                raise ClockError(
                    f"clock arithmetic says {mover_seat} flagged before ply {receipt.ply}"
                )
            if actual_after != expected_after:
                raise ClockError(
                    f"receipt clock for {mover_seat} at ply {receipt.ply} is "
                    f"{actual_after}, expected {expected_after}"
                )
            idle = opposite(mover_seat)
            actual_idle = white_after if idle == "white" else black_after
            if actual_idle != running[idle]:
                raise ClockError("receipt changed the clock of the idle side")
        running[mover_seat] = actual_after
        previous_received = receipt.received_at_ms

    if previous_received is not None:
        final_elapsed = final.received_at_ms - previous_received
        if final_elapsed < 0:
            raise ClockError("flag receipt predates the last move receipt")
        expected_flagged = running[flagged] - max(0, final_elapsed - lag_grace_ms)
        if expected_flagged > 0:
            raise ClockError(
                "flag receipt arrives before the flagged clock reaches zero "
                f"({expected_flagged} ms remaining)"
            )
        actual_winner = final.white_ms if winner_seat == "white" else final.black_ms
        if actual_winner != running[winner_seat]:
            raise ClockError("flag receipt changed the winner's clock")
    else:
        # No post-opening moves: the winner must still be at the initial clock.
        # A normal relay rejects this one-ply case at flag time.
        if winner_ms != control.initial_ms:
            raise ClockError("flag receipt without move receipts changed the winner's clock")


def verify_timeout_claim(
    claim: TimeoutClaim,
    *,
    certificate: MatchCertificate,
    san_moves: Sequence[str],
    public_keys: Mapping[str, PublicKeyLike | str],
    quorum: int,
    max_plies: int,
    lag_grace_ms: int = DEFAULT_LAG_GRACE_MS,
) -> Color:
    """Verify a bonded timeout claim and return the winner's seat.

    The claim is the refused-result path: the winner presents its last signed
    move plus a complete clock bundle. Chain-level checks (bond locking, the
    dispute window and the claim registry) belong to the transaction layer; this
    function validates every field the chain would otherwise have to trust.
    """
    if claim.bond <= 0:
        raise ClockError("a timeout claim must lock a positive bond")
    if claim.network != certificate.network:
        raise ClockError("timeout claim and certificate name different networks")
    if claim.match_id != certificate.match_id:
        raise ClockError("timeout claim does not belong to this certificate")
    if claim.certificate != certificate:
        raise ClockError("timeout claim embeds a different certificate")
    if not san_moves:
        raise ClockError("a timeout claim needs at least one played move")

    last_move = claim.last_move
    last_ply = len(san_moves) - 1
    if last_move.ply != last_ply:
        raise ClockError(f"last move is ply {last_move.ply}, but the game ends at ply {last_ply}")
    if last_move.san != san_moves[last_ply]:
        raise ClockError(
            f"last move SAN is {last_move.san!r}, the PGN says {san_moves[last_ply]!r}"
        )
    verify_move_signature(last_move, certificate=certificate, public_keys=public_keys)

    final = claim.clock_evidence.final
    if final.seat == certificate.white:
        flagged: Color = "white"
    elif final.seat == certificate.black:
        flagged = "black"
    else:
        raise ClockError("flag receipt does not name a seat of this certificate")
    winner_seat = opposite(flagged)
    result: ResultValue = "1-0" if winner_seat == "white" else "0-1"
    verify_clock_evidence(
        claim.clock_evidence,
        certificate=certificate,
        san_moves=san_moves,
        result=result,
        winner_seat=winner_seat,
        public_keys=public_keys,
        quorum=quorum,
        max_plies=max_plies,
        lag_grace_ms=lag_grace_ms,
    )
    if last_move.seat != (certificate.white if winner_seat == "white" else certificate.black):
        raise ClockError("the timeout claim's last move was not made by the winner")
    return winner_seat


# Friendly aliases: callers may use any of these names without importing the
# implementation dataclass.
ChessClock = MatchClock
Clock = MatchClock


__all__ = [
    "DEFAULT_LAG_GRACE_MS",
    "FIXED_TIME_CONTROL",
    "MAX_LAG_GRACE_MS",
    "OPPOSITE",
    "ChessClock",
    "Clock",
    "MatchClock",
    "clock_receipt_payload",
    "flag_result",
    "move_payload",
    "opposite",
    "sign_move",
    "sign_receipt",
    "time_control_from_tag",
    "time_control_tag",
    "validate_time_control",
    "verify_clock_evidence",
    "verify_move_evidence",
    "verify_move_signature",
    "verify_receipt_signatures",
    "verify_timeout_claim",
]
