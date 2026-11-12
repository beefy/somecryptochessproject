"""sccp -- a cryptocurrency whose proof of work is winning chess games.

The package is import-light on purpose: importing :mod:`sccp` must not start
engines, open databases or read configuration. Entry points (CLI, node, miner)
call :func:`sccp.version.verify_chess_version` during startup so an unexpected
``python-chess`` upgrade fails loudly instead of silently changing PGN parsing
behaviour (docs/PROTOCOL.md).
"""

from sccp.version import __version__

__all__ = ["__version__"]
