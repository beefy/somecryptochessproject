"""Version constants and runtime dependency guards.

``python-chess`` ships distribution version ``1.999`` while ``chess.__version__``
is ``1.11.2``. The protocol (PROTOCOL.md section 3) relies on the exact 1.11 PGN
semantics -- ``read_game`` consuming one game per call, ``game.errors`` being
necessary but not sufficient, ``push()`` accepting illegal moves -- so startup
asserts the ``chess.__version__`` series rather than the distribution version.
"""

from __future__ import annotations

import re
from typing import Final

__version__: Final = "0.15.0"
"""Package version (kept in sync with ``pyproject.toml``)."""

PROTOCOL_VERSION: Final = 1
"""Consensus wire-format version; part of every hash domain (``sccp/v1``)."""

DOMAIN_PREFIX: Final = f"sccp/v{PROTOCOL_VERSION}"
"""Prefix shared by all hash domains (see :mod:`sccp.serialization`)."""

EXPECTED_CHESS_SERIES: Final = "1.11"
"""``chess.__version__`` major.minor series this codebase is validated against."""

CHESS_SERIES_PATTERN: Final = re.compile(r"^(\d+)\.(\d+)\.")


def chess_version() -> str:
    """Return the installed ``python-chess`` library version (``chess.__version__``)."""
    import chess

    return str(chess.__version__)


def verify_chess_version() -> str:
    """Raise ``RuntimeError`` unless ``python-chess`` is the expected series.

    Returns the installed ``chess.__version__`` so callers can log it.
    """
    version = chess_version()
    match = CHESS_SERIES_PATTERN.match(version)
    if match is None or f"{match.group(1)}.{match.group(2)}" != EXPECTED_CHESS_SERIES:
        raise RuntimeError(
            f"python-chess {version} is not supported: this codebase requires the "
            f"{EXPECTED_CHESS_SERIES}.x behaviour line (see PROTOCOL.md section 3). "
            "Pin python-chess>=1.11,<2."
        )
    return version


def version_string() -> str:
    """Human-readable version line used by ``sccp --version``."""
    return f"sccp {__version__} (protocol v{PROTOCOL_VERSION})"
