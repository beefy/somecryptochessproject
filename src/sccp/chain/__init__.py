"""The ledger: transactions, state, blocks, fork choice, mempool and storage.

Modules (docs/IMPLEMENTATION_PLAN.md section 4 and M3):

- :mod:`sccp.chain.tx` -- transaction validation, application and the txid rule.
- :mod:`sccp.chain.state` -- accounts, bonds, proof registry, epoch records and
  snapshots.
- :mod:`sccp.chain.block` -- header/body validation, merkle roots, seal check.
- :mod:`sccp.chain.chain` -- append, fork choice, reorg and orphan handling.
- :mod:`sccp.chain.mempool` -- tx/proof pools, nonces, fees, expiry, rebroadcast.
- :mod:`sccp.chain.difficulty` -- ``k`` and ``seal_bits`` retargeting.
- :mod:`sccp.chain.storage` -- SQLite persistence, WAL mode and snapshots.
- :mod:`sccp.chain.timeout` -- bonded timeout claims, disputes and finalisation.

Nothing in this package performs network I/O or touches wall-clock time except
where a caller injects an explicit timestamp; every consensus rule is a pure
function of block data and the state it produces.
"""

from __future__ import annotations

__all__ = [
    "block",
    "chain",
    "difficulty",
    "mempool",
    "state",
    "storage",
    "timeout",
    "tx",
]
