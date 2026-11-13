"""Wire models for matchmaking, certificates and win proofs.

The models in this module are the schema half of docs/PROTOCOL.md sections 6-9.
They are frozen, forbid unknown fields and use constrained string types, so a
malformed object fails at the boundary instead of deep inside a verifier.

Signatures are lowercase hex. A ledger transaction carries either the 64-byte
Ed25519 signature (128 hex characters) or the hybrid Ed25519 + ML-DSA-65 blob
(3374 bytes, 6748 hex characters, see :mod:`sccp.crypto.pq`); the leading scheme
byte inside the blob makes the two unambiguous. Match transcripts and clock
receipts remain Ed25519. Digests are lowercase hex of a 32-byte SHA-256 domain
hash. The models deliberately do *not* verify signatures or recompute hashes:
that is the job of :mod:`sccp.protocol.transcript` and
:mod:`sccp.protocol.verification`, which can report a typed failure reason.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from sccp.config import MAX_PLIES_LIMIT

DigestHex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
"""A 32-byte digest or 32-byte nonce, lowercase hex."""

SignatureHex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{128}$")]
"""A 64-byte Ed25519 signature, lowercase hex."""

HybridSignatureHex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{6748}$")]
"""A 3374-byte hybrid Ed25519 + ML-DSA-65 signature blob, lowercase hex."""

TxSignatureHex = SignatureHex | HybridSignatureHex
"""A ledger transaction signature: Ed25519, or hybrid on a version-1 address."""

PublicKeyHex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
"""A raw 32-byte Ed25519 public key, lowercase hex."""

AddressText = Annotated[
    str,
    StringConstraints(min_length=8, max_length=90, pattern=r"^[a-z0-9]+$"),
]
"""A bech32 address. Network membership is checked where the network is known."""

EngineFingerprint = Annotated[
    str,
    StringConstraints(min_length=1, max_length=128, pattern=r"^[\x20-\x7e]+$"),
]
"""A printable engine fingerprint (see docs/PROTOCOL.md section 5)."""

Color = Literal["white", "black"]
ResultValue = Literal["1-0", "0-1", "1/2-1/2"]
TerminationValue = Literal["checkmate", "draw", "resignation", "time_forfeit"]
Verdict = Literal["pass", "fail", "unknown"]

MAX_CLOCK_MS = 24 * 60 * 60 * 1000
"""Upper bound for a clock value (24 hours), from PROTOCOL.md section 8.4."""


class ProtocolModel(BaseModel):
    """Base class: immutable, strict about unknown fields, JSON-friendly."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class TxType(StrEnum):
    """The transaction vocabulary of PROTOCOL.md section 10."""

    TRANSFER = "transfer"
    REGISTER = "register"
    REGISTER_WITNESS = "register_witness"
    UPDATE_REGISTRATION = "update_registration"
    UNREGISTER = "unregister"
    WITHDRAW_BOND = "withdraw_bond"
    AVAILABILITY = "availability"
    REVEAL = "reveal"
    TIMEOUT_CLAIM = "timeout_claim"
    TIMEOUT_DISPUTE = "timeout_dispute"
    CHALLENGE = "challenge"
    SLASH_EVIDENCE = "slash_evidence"


class Transaction(ProtocolModel):
    """A signed ledger transaction (PROTOCOL.md section 10).

    ``payload`` is an untyped mapping on the wire: its shape depends on ``type``
    and is validated by :mod:`sccp.chain.tx`, which keeps one canonical envelope
    and one place to audit per-type rules. The ``sender`` field serializes as
    ``"from"`` because that is the wire spelling; ``from`` is a Python keyword.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    network: str = Field(min_length=1, max_length=32)
    type: TxType
    sender: AddressText = Field(validation_alias="from", serialization_alias="from")
    nonce: int = Field(ge=0)
    fee: int = Field(ge=0)
    payload: dict[str, object] = Field(default_factory=dict)
    signature: TxSignatureHex | None = None


class TimeControl(ProtocolModel):
    """One fixed chess clock control (PROTOCOL.md section 8.4).

    Both values are whole seconds expressed in milliseconds, so the PGN
    ``TimeControl`` tag is lossless. The complete ``initial + increment * plies``
    bound is checked against the network's ``max_plies`` by
    :func:`sccp.protocol.clocks.validate_time_control`; this model enforces the
    protocol-wide worst case as soon as it is constructed.
    """

    initial_ms: int = Field(ge=1_000, multiple_of=1_000)
    increment_ms: int = Field(ge=0, multiple_of=1_000)

    @model_validator(mode="after")
    def _check_worst_case(self) -> TimeControl:
        if self.initial_ms + self.increment_ms * MAX_PLIES_LIMIT > MAX_CLOCK_MS:
            raise ValueError(
                "time control exceeds MAX_CLOCK_MS for max_plies="
                f"{MAX_PLIES_LIMIT}: {self.initial_ms} + {self.increment_ms} * "
                f"{MAX_PLIES_LIMIT} > {MAX_CLOCK_MS}"
            )
        return self


class Availability(ProtocolModel):
    """A miner's commit-phase availability for one epoch (PROTOCOL.md section 6)."""

    network: str = Field(min_length=1, max_length=32)
    miner: AddressText
    epoch: int = Field(ge=0)
    max_games: int = Field(ge=1)
    commitment: DigestHex


class Reveal(ProtocolModel):
    """The reveal-phase nonce matching an :class:`Availability` commitment."""

    network: str = Field(min_length=1, max_length=32)
    miner: AddressText
    epoch: int = Field(ge=0)
    nonce: DigestHex


class BeaconReveal(ProtocolModel):
    """One valid reveal as it enters the beacon payload."""

    miner: AddressText
    commitment: DigestHex
    nonce: DigestHex


class Beacon(ProtocolModel):
    """A derived epoch beacon together with the public data it commits to."""

    network: str = Field(min_length=1, max_length=32)
    epoch: int = Field(ge=0)
    hash: DigestHex
    block_hash: DigestHex
    reveals: tuple[BeaconReveal, ...] = ()
    drand_used: bool = False
    drand_round: int | None = Field(default=None, ge=0)
    drand_signature: str | None = Field(
        default=None,
        min_length=2,
        max_length=256,
        pattern=r"^[0-9a-f]+$",
    )


class OpeningRef(ProtocolModel):
    """The book line chosen for a match and the hash that commits to it."""

    book_version: str = Field(min_length=1, max_length=64)
    line_id: int = Field(ge=0)
    hash: DigestHex
    moves: tuple[str, ...] = Field(min_length=1, max_length=12)


class Matchup(ProtocolModel):
    """One round pairing: who plays whom and with which colour."""

    network: str = Field(min_length=1, max_length=32)
    match_id: DigestHex
    epoch: int = Field(ge=0)
    round: int = Field(ge=0)
    white: AddressText
    black: AddressText

    @property
    def seats(self) -> tuple[str, str]:
        """The two seat addresses in canonical (sorted) order."""
        return tuple(sorted((self.white, self.black)))  # type: ignore[return-value]


class MatchCertificate(ProtocolModel):
    """The signed start-of-match commitment (PROTOCOL.md section 8.1).

    ``start_signatures`` is keyed by colour and contains one signature per seat
    over :func:`sccp.protocol.transcript.certificate_payload`.
    """

    network: str = Field(min_length=1, max_length=32)
    match_id: DigestHex
    epoch: int = Field(ge=0)
    round: int = Field(ge=0)
    white: AddressText
    black: AddressText
    opening: OpeningRef
    engine_white: EngineFingerprint
    engine_black: EngineFingerprint
    time_control: TimeControl = Field(
        default_factory=lambda: TimeControl(initial_ms=1_800_000, increment_ms=0)
    )
    start_signatures: dict[Color, SignatureHex] = Field(default_factory=dict)


class ClockWitnessSignature(ProtocolModel):
    """One registered witness's signature over a clock receipt."""

    witness: AddressText
    signature: SignatureHex


class SignedMove(ProtocolModel):
    """A seat-signed move message (PROTOCOL.md section 8.4).

    ``clock_ms`` is deliberately absent: it is relay display data, never signed,
    and is carried by :class:`MoveMessage` for transport only.
    """

    network: str = Field(min_length=1, max_length=32)
    match_id: DigestHex
    seat: AddressText
    ply: int = Field(ge=0)
    san: str = Field(min_length=1, max_length=16)
    signature: SignatureHex


class MoveMessage(SignedMove):
    """The relay's wire form of a move, including its display clock."""

    clock_ms: int = Field(ge=0, le=MAX_CLOCK_MS)


class MoveReceipt(ProtocolModel):
    """A witness-signed receipt for one accepted post-opening move."""

    kind: Literal["move"] = "move"
    match_id: DigestHex
    ply: int = Field(ge=0)
    seat: AddressText
    san: str = Field(min_length=1, max_length=16)
    received_at_ms: int = Field(ge=0)
    white_ms: int = Field(ge=0, le=MAX_CLOCK_MS)
    black_ms: int = Field(ge=0, le=MAX_CLOCK_MS)
    witnesses: tuple[ClockWitnessSignature, ...] = ()


class FlagReceipt(ProtocolModel):
    """A witness-signed receipt for a flag fall (no SAN is present)."""

    kind: Literal["flag"] = "flag"
    match_id: DigestHex
    ply: int = Field(ge=0)
    seat: AddressText
    received_at_ms: int = Field(ge=0)
    white_ms: int = Field(ge=0, le=MAX_CLOCK_MS)
    black_ms: int = Field(ge=0, le=MAX_CLOCK_MS)
    witnesses: tuple[ClockWitnessSignature, ...] = ()


class ClockEvidence(ProtocolModel):
    """Everything a verifier needs to settle a flag (PROTOCOL.md section 8.4)."""

    time_control: TimeControl
    final: FlagReceipt
    receipts: tuple[MoveReceipt, ...] = ()


class TimeoutClaim(ProtocolModel):
    """The payload of a bonded ``timeout_claim`` transaction."""

    network: str = Field(min_length=1, max_length=32)
    match_id: DigestHex
    certificate: MatchCertificate
    last_move: SignedMove
    clock_evidence: ClockEvidence
    bond: int = Field(gt=0)


class GameResult(ProtocolModel):
    """Both seats' attestation of a finished game (PROTOCOL.md section 8.2).

    This is the protocol's result object, distinct from
    :class:`sccp.chess.pgn.GameResult` (the PGN result vocabulary). Use
    ``GameResult.result`` for the ``"1-0"``/``"0-1"``/``"1/2-1/2"`` value.
    """

    network: str = Field(min_length=1, max_length=32)
    match_id: DigestHex
    game_hash: DigestHex
    result: ResultValue
    termination: TerminationValue
    plies: int = Field(ge=1)
    result_signatures: dict[Color, SignatureHex] = Field(default_factory=dict)


class QualitySummary(ProtocolModel):
    """Optional quality verdict attached to a proof (PROTOCOL.md section 9.2).

    The summary is non-consensus evidence: it names the policy, records the
    compact counters a validator can compare against a re-run, and commits to the
    full trace by hash. Every counter defaults to zero so older proofs and
    malformed-but-parseable summaries remain structurally valid.
    """

    policy: str = Field(min_length=1, max_length=64)
    verdict: Verdict
    trace_hash: DigestHex | None = None
    engine_fingerprint: str | None = Field(default=None, max_length=128)
    white_blunders: int = Field(default=0, ge=0)
    black_blunders: int = Field(default=0, ge=0)
    worst_cpl: int = Field(default=0, ge=0)


class WinProof(ProtocolModel):
    """A winner's self-contained claim (PROTOCOL.md section 9)."""

    network: str = Field(min_length=1, max_length=32)
    proof_id: DigestHex
    game_hash: DigestHex
    match_id: DigestHex
    epoch: int = Field(ge=0)
    round: int = Field(ge=0)
    winner: AddressText
    loser: AddressText
    winner_seat: Color
    result: ResultValue
    termination: TerminationValue
    plies: int = Field(ge=1)
    low_ply: bool = False
    opening: OpeningRef
    pgn: str = Field(min_length=1, max_length=262_144)
    certificate: MatchCertificate
    result_signatures: dict[Color, SignatureHex] = Field(default_factory=dict)
    clock_evidence: ClockEvidence | None = None
    move_evidence: tuple[SignedMove, ...] | None = None
    timeout_claim_id: DigestHex | None = None
    quality_summary: QualitySummary | None = None


class BlockHeader(ProtocolModel):
    """The sealed header of PROTOCOL.md sections 12-13, in wire order."""

    version: int = Field(ge=1)
    network: str = Field(min_length=1, max_length=32)
    height: int = Field(ge=0)
    prev_hash: DigestHex
    merkle_root: DigestHex
    state_root: DigestHex
    roster_root: DigestHex
    timestamp: int = Field(ge=0)
    epoch: int = Field(ge=0)
    beacon_hash: DigestHex
    wins_required: int = Field(ge=1)
    seal_bits: int = Field(ge=0, le=32)
    seal_nonce: int = Field(ge=0)
    miner: AddressText


class Block(ProtocolModel):
    """A header plus its body: coinbase, transactions and win proofs.

    Genesis carries neither a coinbase nor proofs (PROTOCOL.md sections 12-13); every
    other block has exactly one coinbase and exactly ``header.wins_required``
    proofs.
    """

    header: BlockHeader
    coinbase: Transaction | None = None
    transactions: tuple[Transaction, ...] = ()
    proofs: tuple[WinProof, ...] = ()


class Transcript(ProtocolModel):
    """A stored match: certificate, co-signed result and full PGN."""

    certificate: MatchCertificate
    result: GameResult
    pgn: str = Field(min_length=1, max_length=262_144)


class RosterMember(ProtocolModel):
    """A roster entry plus its registered public key, used by offline verifiers."""

    public_key: PublicKeyHex
    engine: EngineFingerprint | None = None
    max_games: int = Field(default=1, ge=1)
    funder: str | None = None
    subnet: str | None = None
    asn: int | None = Field(default=None, ge=0)
    recent_opponents: tuple[AddressText, ...] = ()


__all__ = [
    "MAX_CLOCK_MS",
    "MAX_PLIES_LIMIT",
    "AddressText",
    "Availability",
    "Beacon",
    "BeaconReveal",
    "Block",
    "BlockHeader",
    "ClockEvidence",
    "ClockWitnessSignature",
    "Color",
    "DigestHex",
    "EngineFingerprint",
    "FlagReceipt",
    "GameResult",
    "MatchCertificate",
    "Matchup",
    "MoveMessage",
    "MoveReceipt",
    "OpeningRef",
    "ProtocolModel",
    "PublicKeyHex",
    "QualitySummary",
    "ResultValue",
    "Reveal",
    "RosterMember",
    "SignatureHex",
    "SignedMove",
    "TerminationValue",
    "TimeControl",
    "TimeoutClaim",
    "Transaction",
    "Transcript",
    "TxType",
    "Verdict",
    "WinProof",
]
