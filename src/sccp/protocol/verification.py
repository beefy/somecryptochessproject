"""Offline win-proof verification (section 5.10, cheap checks first).

``verify_win_proof`` never trusts a field it can recompute. It orders checks from
cheapest to most expensive -- network, beacon, certificate shape, opening,
roster/pairing, signatures, PGN replay, hashes, duplicates -- and raises
:class:`~sccp.errors.ProofVerificationError` with a stable
:class:`ProofFailure` reason at the first failure.

Quality sampling is deliberately absent: it is M5 work and must never be a
consensus rule. A proof that passes here is structurally and cryptographically
valid; whether the chess was played well is a separate question.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import NoReturn

from sccp.chess.openings import OpeningBook, book_for
from sccp.chess.pgn import GameResult as PgnResult
from sccp.chess.pgn import ParsedGame, validate_pgn
from sccp.config import NetworkParams, get_network
from sccp.crypto.keys import PublicKeyLike
from sccp.errors import (
    BeaconError,
    NetworkError,
    OpeningBookError,
    PairingError,
    PgnValidationError,
    ProofVerificationError,
    ProtocolError,
    TranscriptError,
)
from sccp.protocol.beacon import DrandVerifier, verify_beacon
from sccp.protocol.models import (
    Beacon,
    GameResult,
    Matchup,
    RosterMember,
    WinProof,
)
from sccp.protocol.pairing import RosterEntry, match_id, verify_pairing, white_seat
from sccp.protocol.transcript import (
    expected_from_certificate,
    validate_opening_ref,
    verify_certificate_signatures,
    verify_result_signatures,
)
from sccp.serialization import Domain, domain_hex


class ProofFailure(StrEnum):
    """Stable machine-readable reasons a proof can fail."""

    CONTEXT = "context"
    NETWORK = "network"
    BEACON = "beacon"
    CERTIFICATE = "certificate"
    OPENING = "opening"
    ROSTER = "roster"
    PAIRING = "pairing"
    SIGNATURE = "signature"
    PGN = "pgn"
    GAME_HASH = "game-hash"
    RESULT = "result"
    PROOF_ID = "proof-id"
    DUPLICATE = "duplicate"


def _fail(reason: ProofFailure, message: str) -> NoReturn:
    raise ProofVerificationError(reason, message)


@dataclass(frozen=True, slots=True)
class ProofContext:
    """Everything a verifier knows independently of the proof itself."""

    params: NetworkParams
    beacon: Beacon
    roster: Mapping[str, RosterEntry]
    public_keys: Mapping[str, PublicKeyLike | str]
    book: OpeningBook | None = None
    quotas: Mapping[str, int] | None = None
    availability: Mapping[str, str] | None = None
    drand_verifier: DrandVerifier | None = None
    expected_drand_round: int | None = None
    seen_game_hashes: frozenset[str] = field(default_factory=frozenset)
    seen_proof_ids: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True, slots=True)
class VerifiedProof:
    """A proof that passed every check, together with its replayed game."""

    proof: WinProof
    parsed: ParsedGame


def verify_win_proof(proof: WinProof, context: ProofContext) -> VerifiedProof:
    """Verify a win proof or raise :class:`ProofVerificationError`.

    ``context`` supplies the independent facts: network parameters, the epoch
    beacon, the roster with registered keys, and optionally the opening book and
    already-seen hashes.
    """
    params = context.params
    cert = proof.certificate
    beacon = context.beacon

    # 1. Network binding, before anything expensive.
    if proof.network != params.network_id:
        _fail(
            ProofFailure.NETWORK,
            f"proof is for {proof.network!r}, verifier is on {params.network_id!r}",
        )
    if cert.network != proof.network:
        _fail(
            ProofFailure.NETWORK,
            f"certificate is for {cert.network!r}, proof is for {proof.network!r}",
        )
    if beacon.network != proof.network:
        _fail(
            ProofFailure.NETWORK,
            f"beacon is for {beacon.network!r}, proof is for {proof.network!r}",
        )
    if beacon.epoch != proof.epoch:
        _fail(
            ProofFailure.BEACON,
            f"beacon is for epoch {beacon.epoch}, proof claims epoch {proof.epoch}",
        )

    # 2. Beacon: recompute from reveals, availability and (optionally) drand.
    availability = dict(context.availability) if context.availability is not None else None
    try:
        verify_beacon(
            beacon,
            availability=availability,
            drand_verifier=context.drand_verifier,
            expected_round=context.expected_drand_round,
        )
    except BeaconError as exc:
        _fail(ProofFailure.BEACON, str(exc))
    beacon_bytes = bytes.fromhex(beacon.hash)

    # 3. Certificate shape, match id and colour draw.
    if cert.match_id != proof.match_id or cert.epoch != proof.epoch or cert.round != proof.round:
        _fail(
            ProofFailure.CERTIFICATE,
            "certificate epoch/round/match id do not match the proof",
        )
    if cert.white == cert.black:
        _fail(ProofFailure.CERTIFICATE, "certificate seats must be distinct")
    if proof.round >= params.rounds_per_epoch:
        _fail(
            ProofFailure.CERTIFICATE,
            f"round {proof.round} is outside the {params.rounds_per_epoch}-round epoch",
        )
    if proof.winner not in (cert.white, cert.black) or proof.loser not in (
        cert.white,
        cert.black,
    ):
        _fail(ProofFailure.CERTIFICATE, "winner and loser must be the two seats")
    if proof.winner == proof.loser:
        _fail(ProofFailure.CERTIFICATE, "winner and loser must be different seats")
    winner_seat = "white" if proof.winner == cert.white else "black"
    expected_loser = cert.black if winner_seat == "white" else cert.white
    if proof.winner_seat != winner_seat:
        _fail(
            ProofFailure.CERTIFICATE,
            f"proof says the winner sat {proof.winner_seat}, the certificate says {winner_seat}",
        )
    if proof.loser != expected_loser:
        _fail(ProofFailure.CERTIFICATE, "loser is not the other certificate seat")
    seats = sorted((cert.white, cert.black))
    try:
        recomputed_match = match_id(proof.network, proof.epoch, proof.round, seats)
    except PairingError as exc:
        _fail(ProofFailure.CERTIFICATE, str(exc))
    if recomputed_match != proof.match_id:
        _fail(
            ProofFailure.CERTIFICATE,
            f"match id is {proof.match_id}, but the seats recompute to {recomputed_match}",
        )
    if white_seat(beacon_bytes, proof.match_id, seats) != cert.white:
        _fail(ProofFailure.CERTIFICATE, "certificate colours do not match the beacon draw")

    # 4. Opening: the certificate carries the line, so no local book is needed.
    if proof.opening != cert.opening:
        _fail(ProofFailure.OPENING, "proof opening does not match the certificate")
    try:
        validate_opening_ref(proof.opening)
    except TranscriptError as exc:
        _fail(ProofFailure.OPENING, str(exc))
    if context.book is not None:
        try:
            line = context.book.line(proof.opening.line_id)
        except OpeningBookError as exc:
            _fail(ProofFailure.OPENING, str(exc))
        if (
            line.book_version != proof.opening.book_version
            or line.moves != proof.opening.moves
            or line.opening_hash != proof.opening.hash
        ):
            _fail(ProofFailure.OPENING, "opening does not match the local book line")
        if context.book.select(beacon_bytes, proof.match_id) != line:
            _fail(ProofFailure.OPENING, "beacon did not select this opening line")

    # 5. Roster membership and the pairing the beacon produced.
    for seat in (cert.white, cert.black):
        entry = context.roster.get(seat)
        if entry is None or entry.address != seat:
            _fail(ProofFailure.ROSTER, f"seat {seat} is not in the epoch roster")
    matchup = Matchup(
        network=proof.network,
        match_id=proof.match_id,
        epoch=proof.epoch,
        round=proof.round,
        white=cert.white,
        black=cert.black,
    )
    if not verify_pairing(
        matchup,
        list(context.roster.values()),
        beacon_bytes,
        rounds_per_epoch=params.rounds_per_epoch,
        quotas=context.quotas,
        opponent_cooldown=params.opponent_cooldown,
    ):
        _fail(ProofFailure.PAIRING, "matchup is not the one this roster and beacon produce")

    # 6. Both seats must have signed the start and the result.
    result = GameResult(
        network=proof.network,
        match_id=proof.match_id,
        game_hash=proof.game_hash,
        result=proof.result,
        termination=proof.termination,
        plies=proof.plies,
        result_signatures=proof.result_signatures,
    )
    try:
        verify_certificate_signatures(cert, context.public_keys)
        verify_result_signatures(result, cert, context.public_keys)
    except (TranscriptError, ValueError) as exc:
        _fail(ProofFailure.SIGNATURE, str(exc))

    # 7. Replay the PGN against the certificate's opening and result.
    try:
        parsed = validate_pgn(
            proof.pgn,
            params=params,
            expected=expected_from_certificate(cert),
            require_win=True,
        )
    except PgnValidationError as exc:
        _fail(ProofFailure.PGN, str(exc))

    # 8. Hashes and declared fields must match the replay.
    if parsed.game_hash != proof.game_hash:
        _fail(
            ProofFailure.GAME_HASH,
            f"PGN replays to {parsed.game_hash}, proof claims {proof.game_hash}",
        )
    if (
        parsed.result.value != proof.result
        or parsed.termination.value != proof.termination
        or parsed.plies != proof.plies
    ):
        _fail(
            ProofFailure.RESULT,
            "proof result/termination/plies do not match the replayed game",
        )
    parsed_seat = "white" if parsed.result is PgnResult.WHITE else "black"
    if parsed_seat != proof.winner_seat:
        _fail(ProofFailure.RESULT, "winner seat does not match the replayed result")
    expected_proof_id = domain_hex(
        Domain.PROOF,
        {
            "game_hash": proof.game_hash,
            "match_id": proof.match_id,
            "winner": proof.winner,
        },
    )
    if expected_proof_id != proof.proof_id:
        _fail(
            ProofFailure.PROOF_ID,
            f"proof id is {proof.proof_id}, but its fields recompute to {expected_proof_id}",
        )

    # 9. Uniqueness is the caller's chain state.
    if proof.game_hash in context.seen_game_hashes:
        _fail(ProofFailure.DUPLICATE, f"game {proof.game_hash} has already been claimed")
    if proof.proof_id in context.seen_proof_ids:
        _fail(ProofFailure.DUPLICATE, f"proof {proof.proof_id} has already been submitted")

    return VerifiedProof(proof=proof, parsed=parsed)


def _context_error(message: str) -> NoReturn:
    raise ProofVerificationError(ProofFailure.CONTEXT, f"invalid proof context: {message}")


def _digest_set(document: Mapping[str, object], key: str) -> frozenset[str]:
    raw = document.get(key, [])
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        _context_error(f"{key} must be a list of hex digests")
    for item in raw:
        if len(item) != 64:
            _context_error(f"{key} entry {item!r} is not a 32-byte hex digest")
        try:
            bytes.fromhex(item)
        except ValueError:
            _context_error(f"{key} entry {item!r} is not valid hex")
    return frozenset(raw)


def context_from_document(
    document: Mapping[str, object],
    *,
    params: NetworkParams | None = None,
    book: OpeningBook | None = None,
    drand_verifier: DrandVerifier | None = None,
    load_book: bool = True,
) -> ProofContext:
    """Build a :class:`ProofContext` from the JSON used by ``sccp verify proof``.

    The document carries only public data::

        {"network": "testnet",
         "beacon": {...Beacon...},
         "roster": {"sccpt1...": {"public_key": "...", "max_games": 3}},
         "availability": {"sccpt1...": "<commitment>"},
         "quotas": {"sccpt1...": 2},
         "seen_game_hashes": [...],
         "seen_proof_ids": [...]}

    drand verification needs a callable, so it cannot come from JSON; pass
    ``drand_verifier`` when the beacon uses drand.
    """
    if not isinstance(document, Mapping):
        _context_error("document must be a JSON object")
    network = document.get("network")
    if not isinstance(network, str) or not network:
        _context_error("network must be a non-empty string")
    if params is None:
        try:
            params = get_network(network)
        except (NetworkError, ProtocolError) as exc:
            _context_error(str(exc))
    elif params.network_id != network:
        _context_error(f"document is for {network!r}, verifier was asked for {params.network_id!r}")

    beacon_document = document.get("beacon")
    if not isinstance(beacon_document, Mapping):
        _context_error("beacon must be an object")
    try:
        beacon = Beacon.model_validate(beacon_document)
    except ValueError as exc:
        _context_error(f"beacon is invalid: {exc}")

    roster_document = document.get("roster")
    if not isinstance(roster_document, Mapping) or not roster_document:
        _context_error("roster must be a non-empty object keyed by address")
    roster: dict[str, RosterEntry] = {}
    public_keys: dict[str, str] = {}
    for address, member_document in roster_document.items():
        if not isinstance(address, str) or not isinstance(member_document, Mapping):
            _context_error("roster entries must map an address to an object")
        try:
            member = RosterMember.model_validate(member_document)
        except ValueError as exc:
            _context_error(f"roster entry {address} is invalid: {exc}")
        roster[address] = RosterEntry(
            address=address,
            max_games=member.max_games,
            funder=member.funder,
            subnet=member.subnet,
            asn=member.asn,
            recent_opponents=member.recent_opponents,
        )
        public_keys[address] = member.public_key

    availability: dict[str, str] | None = None
    raw_availability = document.get("availability")
    if raw_availability is not None:
        if not isinstance(raw_availability, Mapping):
            _context_error("availability must be an object")
        availability = {}
        for address, commitment_hex in raw_availability.items():
            if not isinstance(address, str) or not isinstance(commitment_hex, str):
                _context_error("availability must map addresses to commitment hex")
            if len(commitment_hex) != 64:
                _context_error(f"availability commitment for {address} is not 32 bytes")
            availability[address] = commitment_hex

    quotas: dict[str, int] | None = None
    raw_quotas = document.get("quotas")
    if raw_quotas is not None:
        if not isinstance(raw_quotas, Mapping):
            _context_error("quotas must be an object")
        quotas = {}
        for address, quota in raw_quotas.items():
            if (
                not isinstance(address, str)
                or not isinstance(quota, int)
                or isinstance(quota, bool)
            ):
                _context_error("quotas must map addresses to integers")
            if quota < 0:
                _context_error(f"quota for {address} must not be negative")
            quotas[address] = quota

    expected_round: int | None = None
    raw_round = document.get("drand_round")
    if raw_round is not None:
        if not isinstance(raw_round, int) or isinstance(raw_round, bool) or raw_round < 0:
            _context_error("drand_round must be a non-negative integer")
        expected_round = raw_round

    if book is None and load_book:
        try:
            book = book_for(params)
        except (OpeningBookError, OSError) as exc:
            _context_error(f"cannot load the opening book: {exc}")

    return ProofContext(
        params=params,
        beacon=beacon,
        roster=roster,
        public_keys=public_keys,
        book=book,
        quotas=quotas,
        availability=availability,
        drand_verifier=drand_verifier,
        expected_drand_round=expected_round,
        seen_game_hashes=_digest_set(document, "seen_game_hashes"),
        seen_proof_ids=_digest_set(document, "seen_proof_ids"),
    )


__all__ = [
    "ProofContext",
    "ProofFailure",
    "VerifiedProof",
    "context_from_document",
    "verify_win_proof",
]
