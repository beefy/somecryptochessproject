"""Independent clock witnesses (PROTOCOL.md section 8.5).

A witness observes a relay's match stream, checks the clock arithmetic the
protocol defines, and signs the receipts and the window checkpoint with its own
key. The relay is a convenience, not an authority: its receipts are only worth
what a witness's signature makes them worth, and a deployment with one witness is
one signature away from losing every timeout it has to settle.

:mod:`sccp.witness.service` is that observer/signer; ``sccp witness run`` serves
it, and ``sccp miner run --witness-url`` falls back to its bundles when the relay
cannot produce one.
"""

from sccp.witness.service import WitnessError, WitnessService, create_witness_app

__all__ = ["WitnessError", "WitnessService", "create_witness_app"]
