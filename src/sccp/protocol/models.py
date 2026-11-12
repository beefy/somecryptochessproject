"""Wire models for matchmaking, certificates and win proofs (M2).

The models in this module are the schema half of docs/PROTOCOL.md sections 6-9.
They are frozen, forbid unknown fields and use constrained string types, so a
malformed object fails at the boundary instead of deep inside a verifier.

Signatures are always lowercase hex of the 64-byte Ed25519 signature; digests are
lowercase hex of a 32-byte SHA-256 domain hash. The models deliberately do *not*
verify signatures or recompute hashes: that is the job of
:mod:`sccp.protocol.transcript` and :mod:`sccp.protocol.verification`, which can
report a typed failure reason.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

DigestHex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
"""A 32-byte digest or 32-byte nonce, lowercase hex."""

SignatureHex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{128}$")]
"""A 64-byte Ed25519 signature, lowercase hex."""

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
TerminationValue = Literal["checkmate", "draw", "resignation"]
Verdict = Literal["pass", "fail", "unknown"]


class ProtocolModel(BaseModel):
    """Base class: immutable, strict about unknown fields, JSON-friendly."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class Availability(ProtocolModel):
    """A miner's commit-phase availability for one epoch (section 5.5)."""

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
    """The signed start-of-match commitment (section 5.7, step 1).

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
    start_signatures: dict[Color, SignatureHex] = Field(default_factory=dict)


class GameResult(ProtocolModel):
    """Both seats' attestation of a finished game (section 5.7, step 3).

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
    """Optional quality verdict attached to a proof; filled in by M5."""

    policy: str = Field(min_length=1, max_length=64)
    verdict: Verdict
    trace_hash: DigestHex | None = None


class WinProof(ProtocolModel):
    """A winner's self-contained claim (section 5.10)."""

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
    opening: OpeningRef
    pgn: str = Field(min_length=1, max_length=262_144)
    certificate: MatchCertificate
    result_signatures: dict[Color, SignatureHex] = Field(default_factory=dict)
    quality_summary: QualitySummary | None = None


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
    "AddressText",
    "Availability",
    "Beacon",
    "BeaconReveal",
    "Color",
    "DigestHex",
    "EngineFingerprint",
    "GameResult",
    "MatchCertificate",
    "Matchup",
    "OpeningRef",
    "ProtocolModel",
    "PublicKeyHex",
    "QualitySummary",
    "ResultValue",
    "Reveal",
    "RosterMember",
    "SignatureHex",
    "TerminationValue",
    "Transcript",
    "Verdict",
    "WinProof",
]
