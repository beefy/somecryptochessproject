"""Typed errors.

Every error raised on purpose by this package derives from :class:`SccpError`.
Where it is natural, the hierarchy also subclasses the built-in exception a
caller would expect (``ValueError`` for bad values, ``TypeError`` for wrong
types), so ``except ValueError`` keeps working while tests can still assert on a
specific type. See docs/IMPLEMENTATION_PLAN.md section 5.2.
"""

from __future__ import annotations


class SccpError(Exception):
    """Base class for all errors raised by this package."""


# --- configuration -----------------------------------------------------------


class ConfigError(SccpError, ValueError):
    """Invalid network parameters or configuration."""


class NetworkError(ConfigError):
    """Unknown network id."""


# --- serialization -----------------------------------------------------------


class SerializationError(SccpError, ValueError):
    """A value cannot be represented in canonical JSON."""


class RangeError(SerializationError):
    """An integer falls outside the consensus integer range."""


class DuplicateKeyError(SerializationError):
    """Two object keys collide after Unicode normalization."""


class UnsupportedTypeError(SerializationError, TypeError):
    """A value of an unsupported Python type reached the encoder."""


# --- keys, addresses and wallets --------------------------------------------


class KeyEncodingError(SccpError, ValueError):
    """Key material has the wrong length or encoding."""


class AddressError(SccpError, ValueError):
    """A bech32 address is malformed, non-canonical or for another network."""


class KeyfileError(SccpError, ValueError):
    """A wallet keyfile is malformed or fails authentication."""


class PassphraseError(KeyfileError):
    """The supplied passphrase cannot decrypt the keyfile."""


# --- chess -------------------------------------------------------------------


class ChessError(SccpError, ValueError):
    """Base class for chess-core failures."""


class PgnValidationError(ChessError):
    """A PGN is malformed, inconsistent or not a valid sccp game."""


class GameError(ChessError):
    """The game runner observed a rule violation, such as an illegal move."""


class OpeningBookError(ChessError):
    """The opening book is missing, malformed or violates the book rules."""


class EngineError(SccpError, RuntimeError):
    """A chess engine crashed, violated UCI or returned an illegal move."""


class EngineFetchError(EngineError):
    """An engine download failed its integrity check."""


# --- protocol ----------------------------------------------------------------


class ProtocolError(SccpError, ValueError):
    """Base class for protocol-level validation failures."""


class BeaconError(ProtocolError):
    """A beacon, commitment or reveal set is malformed or fails verification."""


class PairingError(ProtocolError):
    """A matchup cannot be recomputed from the roster and beacon."""


class TranscriptError(ProtocolError):
    """A certificate, result attestation or transcript is inconsistent."""


class ProofVerificationError(ProtocolError):
    """A win proof failed verification.

    ``reason`` is a stable machine-readable code from
    :class:`sccp.protocol.verification.ProofFailure`; callers should branch on
    that rather than on the message text.
    """

    def __init__(self, reason: object, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class RelayError(ProtocolError):
    """The match relay rejected a request (bad seat, illegal move, wrong state)."""
