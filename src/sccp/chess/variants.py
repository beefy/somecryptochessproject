"""Chess variant helpers: rules, start positions and deterministic selection.

Standard chess always starts from :data:`chess.STARTING_FEN`. Chess960 chooses
one of 960 legal back-rank layouts and keeps the castling rights that layout
implies; its start position must travel with the game because a PGN without it
is ambiguous.

Nothing here is engine-specific. The relay uses
:func:`select_chess960_start` to pick a position from the beacon, the miner uses
:func:`board_for_variant` to rebuild the same board, and PGN validation uses
:func:`normalise_start_fen` to reject a start position that is not one of the
960.
"""

from __future__ import annotations

import hashlib
from typing import Final

import chess

from sccp.config import CHESS960_VARIANT, STANDARD_VARIANT, NetworkParams
from sccp.errors import ConfigError

CHESS960_POSITIONS: Final = 960
"""Number of legal Fischer Random start positions."""

_RULES: Final[dict[str, str]] = {
    STANDARD_VARIANT: "chess",
    CHESS960_VARIANT: "chess960",
}
"""The PGN ``Rules`` value each supported variant uses."""


def variant_rules(name: str) -> str:
    """Return the PGN ``Rules`` tag for ``name``."""
    try:
        return _RULES[name]
    except KeyError:
        known = ", ".join(sorted(_RULES))
        raise ConfigError(f"unknown variant {name!r}; known variants: {known}") from None


def is_chess960(name: str) -> bool:
    """True when ``name`` uses Fischer Random rules."""
    return variant_rules(name) == "chess960"


def chess960_start_fen(index: int) -> str:
    """Shredder-FEN of Chess960 position ``index`` (``0..959``)."""
    if not 0 <= index < CHESS960_POSITIONS:
        raise ConfigError(f"Chess960 position must be in [0, {CHESS960_POSITIONS - 1}]")
    return chess.Board.from_chess960_pos(index).shredder_fen()


def _normalise(fen: str) -> str:
    """Strip move counters so two spellings of one start position compare equal."""
    fields = fen.strip().split()
    if len(fields) < 4:
        raise ConfigError(f"not a FEN: {fen!r}")
    return " ".join(fields[:4]) + " 0 1"


_START_POSITIONS: Final[frozenset[str]] = frozenset(
    _normalise(chess960_start_fen(index)) for index in range(CHESS960_POSITIONS)
)
"""Every legal Chess960 start position, keyed by its normalised Shredder-FEN."""


def normalise_start_fen(fen: str | None, variant: str) -> str:
    """Return the variant's canonical start FEN, or raise :class:`ConfigError`.

    Standard chess accepts only the ordinary initial position. Chess960 accepts
    exactly the 960 legal layouts, expressed in Shredder-FEN so the rook files
    behind each castling right are explicit.
    """
    rules = variant_rules(variant)
    if rules == "chess":
        if fen is not None and _normalise(fen) != _normalise(chess.STARTING_FEN):
            raise ConfigError("standard chess games must start from the initial position")
        return chess.STARTING_FEN
    if fen is None:
        raise ConfigError("a Chess960 game needs its start FEN")
    try:
        board = chess.Board(fen, chess960=True)
    except ValueError as exc:
        raise ConfigError(f"invalid Chess960 start FEN {fen!r}: {exc}") from None
    canonical = _normalise(board.shredder_fen())
    if canonical not in _START_POSITIONS:
        raise ConfigError(f"{fen!r} is not one of the 960 Chess960 start positions")
    return canonical


def board_for_variant(variant: str, start_fen: str | None = None) -> chess.Board:
    """Build the initial board for ``variant`` (Chess960 requires ``start_fen``)."""
    canonical = normalise_start_fen(start_fen, variant)
    return chess.Board(canonical, chess960=is_chess960(variant))


def select_chess960_start(beacon: bytes | str, match_id: str) -> int:
    """Derive a start position from the epoch beacon and the match id.

    The relay is not trusted to invent a position: anyone holding the beacon and
    the match id can recompute this index from the certificate.
    """
    if isinstance(beacon, str):
        try:
            beacon_bytes = bytes.fromhex(beacon)
        except ValueError:
            raise ConfigError("beacon hash is not valid hex") from None
    else:
        beacon_bytes = beacon
    if len(beacon_bytes) != 32:
        raise ConfigError(f"beacon must be 32 bytes, got {len(beacon_bytes)}")
    try:
        match_bytes = bytes.fromhex(match_id)
    except ValueError:
        raise ConfigError("match id is not valid hex") from None
    digest = hashlib.sha256(b"sccp/v1/chess960-start" + beacon_bytes + match_bytes).digest()
    return int.from_bytes(digest[:8], "big") % CHESS960_POSITIONS


def rules_for_network(params: NetworkParams, variant: str) -> str:
    """Return the rules for ``variant`` after checking it is on ``params``."""
    params.variant(variant)
    return variant_rules(variant)


__all__ = [
    "CHESS960_POSITIONS",
    "board_for_variant",
    "chess960_start_fen",
    "is_chess960",
    "normalise_start_fen",
    "rules_for_network",
    "select_chess960_start",
    "variant_rules",
]
