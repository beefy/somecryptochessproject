"""Chess core: PGN validation, openings, engines and the game loop.

Import the submodules directly (``from sccp.chess.pgn import validate_pgn``).
This package re-exports nothing, matching :mod:`sccp.crypto`, so that a consumer
which only needs the opening book never pays for the UCI adapter or the game
runner.
"""
