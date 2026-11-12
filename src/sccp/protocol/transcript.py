"""Certificates, result attestations and transcript storage (PROTOCOL.md section 8).

A match transcript is self-contained: the certificate commits to identity,
pairing, colours, opening and engine fingerprints; both seats sign it before the
first move; both sign the result afterwards. The game hash stays separate and
covers only moves, result and network.

Nothing in this module trusts the relay. Every signature is checked against a
registered public key and every derived field (address, opening hash, proof id)
is recomputed rather than copied.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Final, cast

import chess
import chess.pgn

from sccp.chess.openings import OpeningLine
from sccp.chess.pgn import (
    GameExpectations,
    ParsedGame,
    Termination,
    validate_pgn,
)
from sccp.chess.pgn import (
    GameResult as PgnResult,
)
from sccp.config import NetworkParams
from sccp.crypto.address import encode_address
from sccp.crypto.keys import (
    Keypair,
    PublicKeyLike,
    public_key_from_hex,
    sign_domain,
    verify_domain,
)
from sccp.errors import TranscriptError
from sccp.protocol.clocks import FIXED_TIME_CONTROL, time_control_tag
from sccp.protocol.models import (
    ClockEvidence,
    Color,
    GameResult,
    MatchCertificate,
    Matchup,
    OpeningRef,
    QualitySummary,
    ResultValue,
    SignedMove,
    TerminationValue,
    Transcript,
    WinProof,
)
from sccp.serialization import Domain, domain_hex

EVENT_TAG: Final = "sccp match"
SITE_TAG: Final = "?"
DATE_TAG: Final = "????.??.??"
RULES_TAG: Final = "chess"
SEATS: Final[tuple[Color, Color]] = ("white", "black")


def opening_hash(book_version: str, line_id: int, moves: tuple[str, ...] | list[str]) -> str:
    """``H("sccp/v1/opening", {book_version, line_id, moves})`` as hex."""
    return domain_hex(
        Domain.OPENING,
        {
            "book_version": book_version,
            "line_id": line_id,
            "moves": list(moves),
        },
    )


def opening_ref(line: OpeningLine) -> OpeningRef:
    """Turn a loaded book line into the certificate's opening reference."""
    return OpeningRef(
        book_version=line.book_version,
        line_id=line.line_id,
        hash=line.opening_hash,
        moves=line.moves,
    )


def validate_opening_ref(ref: OpeningRef) -> None:
    """Replay the line and check its canonical SAN and hash.

    The certificate carries the line itself, so a verifier never needs a local
    copy of the book to know what prefix the game must have.
    """
    board = chess.Board()
    canonical: list[str] = []
    for number, san in enumerate(ref.moves, start=1):
        if board.is_game_over(claim_draw=False):
            raise TranscriptError(
                f"opening line {ref.line_id} continues after the game ended at ply {number}"
            )
        try:
            move = board.parse_san(san)
        except ValueError:
            raise TranscriptError(
                f"opening line {ref.line_id} has illegal move {san!r} at ply {number}"
            ) from None
        canonical.append(board.san(move))
        board.push(move)
    if tuple(canonical) != tuple(ref.moves):
        raise TranscriptError(
            f"opening line {ref.line_id} is not canonical SAN: {list(ref.moves)} "
            f"should be {canonical}"
        )
    recomputed = opening_hash(ref.book_version, ref.line_id, tuple(ref.moves))
    if recomputed != ref.hash:
        raise TranscriptError(
            f"opening hash is {ref.hash}, but the line recomputes to {recomputed}"
        )


def create_certificate(
    matchup: Matchup,
    opening: OpeningLine | OpeningRef,
    *,
    engine_white: str,
    engine_black: str,
) -> MatchCertificate:
    """Build an unsigned certificate for ``matchup``."""
    ref = opening_ref(opening) if isinstance(opening, OpeningLine) else opening
    validate_opening_ref(ref)
    return MatchCertificate(
        network=matchup.network,
        match_id=matchup.match_id,
        epoch=matchup.epoch,
        round=matchup.round,
        white=matchup.white,
        black=matchup.black,
        opening=ref,
        engine_white=engine_white,
        engine_black=engine_black,
        time_control=FIXED_TIME_CONTROL,
    )


def certificate_payload(cert: MatchCertificate) -> dict[str, object]:
    """The exact payload both seats sign at match start (PROTOCOL.md section 8.1).

    ``seats`` is the sorted address list and ``colors`` maps each colour to its
    seat, so the colour assignment is covered without ambiguity. ``network`` is
    included so a certificate can never be replayed on another chain.
    """
    return {
        "network": cert.network,
        "match_id": cert.match_id,
        "epoch": cert.epoch,
        "round": cert.round,
        "seats": sorted((cert.white, cert.black)),
        "colors": {"white": cert.white, "black": cert.black},
        "opening_hash": cert.opening.hash,
        "time_control": {
            "initial_ms": cert.time_control.initial_ms,
            "increment_ms": cert.time_control.increment_ms,
        },
        "engine_fingerprints": {
            "white": cert.engine_white,
            "black": cert.engine_black,
        },
    }


def result_payload(result: GameResult) -> dict[str, object]:
    """The exact payload both seats sign when the game ends."""
    return {
        "network": result.network,
        "match_id": result.match_id,
        "game_hash": result.game_hash,
        "result": result.result,
        "termination": result.termination,
        "plies": result.plies,
    }


def _seat_address(cert: MatchCertificate, seat: Color) -> str:
    return cert.white if seat == "white" else cert.black


def _require_key_for_seat(cert: MatchCertificate, seat: Color, keypair: Keypair) -> None:
    expected = _seat_address(cert, seat)
    actual = encode_address(keypair.public_key_bytes, cert.network)
    if actual != expected:
        raise TranscriptError(
            f"key belongs to {actual}, but the {seat} seat of the certificate is {expected}"
        )


def sign_certificate(
    cert: MatchCertificate,
    seat: Color,
    keypair: Keypair,
) -> MatchCertificate:
    """Return ``cert`` with ``seat``'s start signature added."""
    _require_key_for_seat(cert, seat, keypair)
    signature = sign_domain(keypair, Domain.CERTIFICATE, certificate_payload(cert)).hex()
    signatures = dict(cert.start_signatures)
    signatures[seat] = signature
    return cert.model_copy(update={"start_signatures": signatures})


def _public_key(value: PublicKeyLike | str) -> PublicKeyLike:
    return public_key_from_hex(value) if isinstance(value, str) else value


def verify_certificate_signatures(
    cert: MatchCertificate,
    public_keys: Mapping[str, PublicKeyLike | str],
) -> None:
    """Check both start signatures against the registered seat keys."""
    payload = certificate_payload(cert)
    for seat in SEATS:
        address = _seat_address(cert, seat)
        signature = cert.start_signatures.get(seat)
        if signature is None:
            raise TranscriptError(f"certificate is missing the {seat} start signature")
        key = public_keys.get(address)
        if key is None:
            raise TranscriptError(f"no registered public key for {address}")
        if not verify_domain(
            _public_key(key), Domain.CERTIFICATE, payload, bytes.fromhex(signature)
        ):
            raise TranscriptError(f"invalid {seat} start signature for {cert.match_id}")


def sign_result(
    result: GameResult,
    cert: MatchCertificate,
    seat: Color,
    keypair: Keypair,
) -> GameResult:
    """Return ``result`` with ``seat``'s result signature added."""
    _require_key_for_seat(cert, seat, keypair)
    if result.match_id != cert.match_id or result.network != cert.network:
        raise TranscriptError("result does not belong to this certificate")
    signature = sign_domain(keypair, Domain.RESULT, result_payload(result)).hex()
    signatures = dict(result.result_signatures)
    signatures[seat] = signature
    return result.model_copy(update={"result_signatures": signatures})


def verify_result_signatures(
    result: GameResult,
    cert: MatchCertificate,
    public_keys: Mapping[str, PublicKeyLike | str],
    *,
    seats: tuple[Color, ...] = SEATS,
) -> None:
    """Check the required result signatures against the registered seat keys.

    A co-signed result passes the default seat pair. The refused-result paths
    pass only the claimant's seat; their separate evidence proves the board or
    the clock.
    """
    if result.match_id != cert.match_id or result.network != cert.network:
        raise TranscriptError("result does not belong to this certificate")
    unknown = sorted(set(result.result_signatures) - set(SEATS))
    if unknown:
        raise TranscriptError(f"result has unknown signature seats: {unknown}")
    payload = result_payload(result)
    for seat in seats:
        address = _seat_address(cert, seat)
        signature = result.result_signatures.get(seat)
        if signature is None:
            raise TranscriptError(f"result is missing the {seat} signature")
        key = public_keys.get(address)
        if key is None:
            raise TranscriptError(f"no registered public key for {address}")
        if not verify_domain(_public_key(key), Domain.RESULT, payload, bytes.fromhex(signature)):
            raise TranscriptError(f"invalid {seat} result signature for {cert.match_id}")


def expected_from_certificate(cert: MatchCertificate) -> GameExpectations:
    """The PGN expectations a game must satisfy to belong to ``cert``."""
    return GameExpectations(
        match_id=cert.match_id,
        epoch=cert.epoch,
        round_number=cert.round,
        seat_white=cert.white,
        seat_black=cert.black,
        engine_white=cert.engine_white,
        engine_black=cert.engine_black,
        opening_id=cert.opening.line_id,
        opening_hash=cert.opening.hash,
        opening_moves=cert.opening.moves,
        time_control=time_control_tag(cert.time_control),
    )


def pgn_headers(cert: MatchCertificate) -> dict[str, str]:
    """Protocol headers for a transcript PGN (section 3.1 of PROTOCOL.md)."""
    return {
        "Event": EVENT_TAG,
        "Site": SITE_TAG,
        "Date": DATE_TAG,
        "Round": str(cert.round),
        "White": cert.white,
        "Black": cert.black,
        "MatchID": cert.match_id,
        "Epoch": str(cert.epoch),
        "RoundNumber": str(cert.round),
        "SeatWhite": cert.white,
        "SeatBlack": cert.black,
        "EngineWhite": cert.engine_white,
        "EngineBlack": cert.engine_black,
        "OpeningID": str(cert.opening.line_id),
        "OpeningHash": cert.opening.hash,
        "TimeControl": time_control_tag(cert.time_control),
        "Rules": RULES_TAG,
    }


def render_transcript_pgn(
    cert: MatchCertificate,
    board: chess.Board,
    *,
    result: PgnResult | str,
    termination: Termination | str,
) -> str:
    """Render a finished board with every protocol header filled in."""
    game = chess.pgn.Game.from_board(board)
    headers = pgn_headers(cert)
    headers["Result"] = PgnResult(result).value
    headers["Termination"] = Termination(termination).value
    for tag, value in headers.items():
        game.headers[tag] = value
    return str(game)


def create_result(
    cert: MatchCertificate,
    parsed: ParsedGame,
    *,
    result_signatures: Mapping[Color, str] | None = None,
) -> GameResult:
    """Build the unsigned result object for a validated game."""
    signatures = dict(result_signatures or {})
    return GameResult(
        network=cert.network,
        match_id=cert.match_id,
        game_hash=parsed.game_hash,
        result=cast(ResultValue, parsed.result.value),
        termination=cast(TerminationValue, parsed.termination.value),
        plies=parsed.plies,
        result_signatures=signatures,
    )


def build_win_proof(
    cert: MatchCertificate,
    pgn_text: str,
    result_signatures: Mapping[Color, str],
    *,
    params: NetworkParams,
    quality_summary: QualitySummary | None = None,
    clock_evidence: ClockEvidence | None = None,
    move_evidence: tuple[SignedMove, ...] | None = None,
    timeout_claim_id: str | None = None,
) -> WinProof:
    """Validate a game and wrap it into a :class:`WinProof`.

    The PGN is replayed and checked against the certificate, so a winner cannot
    submit a proof whose headers, opening or result disagree with what both
    seats signed. The evidence arguments cover the two refused-result paths:
    ``clock_evidence`` plus an optional ``timeout_claim_id`` for a flag, and
    ``move_evidence`` for a refused checkmate.
    """
    if cert.network != params.network_id:
        raise TranscriptError(
            f"certificate is for {cert.network!r}, but params are for {params.network_id!r}"
        )
    for seat in SEATS:
        if seat not in cert.start_signatures:
            raise TranscriptError(f"certificate is missing the {seat} start signature")
    unknown = sorted(set(result_signatures) - set(SEATS))
    if unknown:
        raise TranscriptError(f"unknown result signature seats: {unknown}")

    parsed = validate_pgn(
        pgn_text,
        params=params,
        expected=expected_from_certificate(cert),
        require_win=True,
        clock_evidence=clock_evidence,
    )
    if parsed.result is PgnResult.WHITE:
        winner_seat: Color = "white"
    elif parsed.result is PgnResult.BLACK:
        winner_seat = "black"
    else:  # pragma: no cover - require_win rejects draws
        raise TranscriptError("a win proof needs a decisive result")
    winner = cert.white if winner_seat == "white" else cert.black
    loser = cert.black if winner_seat == "white" else cert.white

    termination = parsed.termination.value
    if clock_evidence is not None and termination != "time_forfeit":
        raise TranscriptError("clock_evidence is only valid for a time forfeit")
    if move_evidence is not None and termination != "checkmate":
        raise TranscriptError("move_evidence is only valid for a checkmate")
    if timeout_claim_id is not None and termination != "time_forfeit":
        raise TranscriptError("timeout_claim_id is only valid for a time forfeit")
    if clock_evidence is not None and clock_evidence.time_control != cert.time_control:
        raise TranscriptError("clock evidence and certificate use different time controls")

    # A missing result signature is only survivable when the accompanying
    # evidence proves the board or the clock. Resignations and draws still need
    # both seats, because they attest intent rather than a position.
    missing = set(SEATS) - set(result_signatures)
    if missing:
        claim_exception = (
            termination == "time_forfeit"
            and timeout_claim_id is not None
            and clock_evidence is not None
            and len(missing) == 1
        )
        checkmate_exception = (
            termination == "checkmate" and move_evidence is not None and len(missing) == 1
        )
        if not (claim_exception or checkmate_exception):
            for seat in SEATS:
                if seat not in result_signatures:
                    raise TranscriptError(f"proof is missing the {seat} result signature")
            raise TranscriptError("proof has an invalid result signature set")  # pragma: no cover
        if winner_seat not in result_signatures:
            raise TranscriptError("the claimant did not sign the result")
        # The signature bytes are checked by the offline verifier, which has the
        # registered keys. Here we only enforce which seats must be present.

    result = create_result(cert, parsed, result_signatures=result_signatures)
    return WinProof(
        network=cert.network,
        proof_id=domain_hex(
            Domain.PROOF,
            {
                "game_hash": parsed.game_hash,
                "match_id": cert.match_id,
                "winner": winner,
            },
        ),
        game_hash=parsed.game_hash,
        match_id=cert.match_id,
        epoch=cert.epoch,
        round=cert.round,
        winner=winner,
        loser=loser,
        winner_seat=winner_seat,
        result=result.result,
        termination=result.termination,
        plies=result.plies,
        low_ply=parsed.low_ply,
        opening=cert.opening,
        pgn=pgn_text,
        certificate=cert,
        result_signatures=dict(result_signatures),
        clock_evidence=clock_evidence,
        move_evidence=move_evidence,
        timeout_claim_id=timeout_claim_id,
        quality_summary=quality_summary,
    )


def proof_from_transcript(
    transcript: Transcript,
    *,
    params: NetworkParams,
    clock_evidence: ClockEvidence | None = None,
    move_evidence: tuple[SignedMove, ...] | None = None,
    timeout_claim_id: str | None = None,
) -> WinProof:
    """Wrap an already co-signed transcript into a proof.

    A co-signed ``time_forfeit`` still needs its clock evidence; the refused
    paths supply the same optional evidence arguments as :func:`build_win_proof`.
    """
    return build_win_proof(
        transcript.certificate,
        transcript.pgn,
        transcript.result.result_signatures,
        params=params,
        clock_evidence=clock_evidence,
        move_evidence=move_evidence,
        timeout_claim_id=timeout_claim_id,
    )


def save_transcript(path: Path, transcript: Transcript) -> None:
    """Write a transcript as canonical-looking JSON, atomically."""
    document = transcript.model_dump(mode="json")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(
            json.dumps(document, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    except OSError as exc:
        raise TranscriptError(f"cannot write transcript {path}: {exc}") from None


def load_transcript(path: Path) -> Transcript:
    """Read and validate a stored transcript."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise TranscriptError(f"cannot read transcript {path}: {exc}") from None
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise TranscriptError(f"transcript {path} is not valid JSON: {exc}") from None
    try:
        return Transcript.model_validate(document)
    except ValueError as exc:
        raise TranscriptError(f"transcript {path} is invalid: {exc}") from None


__all__ = [
    "DATE_TAG",
    "EVENT_TAG",
    "RULES_TAG",
    "SEATS",
    "SITE_TAG",
    "build_win_proof",
    "certificate_payload",
    "create_certificate",
    "create_result",
    "expected_from_certificate",
    "load_transcript",
    "opening_hash",
    "opening_ref",
    "pgn_headers",
    "proof_from_transcript",
    "render_transcript_pgn",
    "result_payload",
    "save_transcript",
    "sign_certificate",
    "sign_result",
    "validate_opening_ref",
    "verify_certificate_signatures",
    "verify_result_signatures",
]
