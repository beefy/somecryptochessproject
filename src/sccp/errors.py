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


class ClockError(ProtocolError):
    """A chess clock operation, receipt or clock-evidence bundle is invalid."""


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


class GossipError(ProtocolError):
    """A gossip envelope, peer table entry or sync response is invalid."""


# --- node --------------------------------------------------------------------


class NodeError(SccpError):
    """A node service request cannot be served."""


class NotFoundError(NodeError):
    """The requested block, account, epoch, match or game is unknown."""


# --- ledger ------------------------------------------------------------------


class LedgerError(SccpError, ValueError):
    """Base class for chain, state, mempool, storage and wallet-service failures."""


class GenesisError(LedgerError):
    """A genesis document is missing, malformed or does not match the pinned hash."""


class TxValidationError(LedgerError):
    """A transaction failed validation.

    ``reason`` is a stable machine-readable code from
    :class:`sccp.chain.tx.TxFailure`; callers should branch on that rather than
    on the message text.
    """

    def __init__(self, reason: object, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class BlockValidationError(LedgerError):
    """A block failed validation.

    ``reason`` is a stable machine-readable code from
    :class:`sccp.chain.block.BlockFailure`.
    """

    def __init__(self, reason: object, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class StateError(LedgerError):
    """A state transition is inconsistent with the ledger rules."""


class MempoolError(LedgerError):
    """A transaction or proof cannot enter the mempool."""


class ChainError(LedgerError):
    """A block cannot be appended, or the chain is inconsistent."""


class StorageError(LedgerError):
    """The SQLite store is corrupt, unreadable or on an unsupported schema."""


class TimeoutClaimError(LedgerError):
    """A bonded timeout claim or dispute is invalid in the current state."""


class WalletServiceError(LedgerError):
    """A wallet-service request is malformed or not affordable."""


# --- devnet ------------------------------------------------------------------


class DevnetError(SccpError):
    """The in-process devnet could not reach its stated goal."""


# --- miner -------------------------------------------------------------------


class MinerError(SccpError, RuntimeError):
    """The miner cannot act safely: refused match, clock, or relay response."""
