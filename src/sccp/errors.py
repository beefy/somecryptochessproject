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


# --- protocol ----------------------------------------------------------------


class ProtocolError(SccpError):
    """Base class for protocol-level validation failures (later milestones)."""
