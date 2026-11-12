"""Matchmaking protocol: wire models, beacons, pairing, transcripts, proofs.

Import the submodules directly (``from sccp.protocol.verification import
verify_win_proof``). This package re-exports nothing, matching
:mod:`sccp.crypto` and :mod:`sccp.chess`, so importing the beacon never drags in
the FastAPI relay or a chess engine.
"""
