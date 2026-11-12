"""Compatibility entry point for the M2 read-only node stub.

M4 moved the API to :mod:`sccp.node.app`, which needs a full
:class:`~sccp.node.service.NodeService`. This module keeps the old signature
alive for callers (and the M2 relay test) that only want to serve a genesis
chain plus a few known beacons and transcripts.
"""

from __future__ import annotations

from collections.abc import Mapping

from fastapi import FastAPI

from sccp.chain.chain import Chain
from sccp.chain.mempool import Mempool
from sccp.config import NetworkParams
from sccp.node.app import create_app as create_node_app
from sccp.node.service import NodeService, default_genesis_path
from sccp.protocol.models import Beacon, Transcript


def create_app(
    params: NetworkParams,
    *,
    beacons: Mapping[int, Beacon] | None = None,
    transcripts: Mapping[str, Transcript] | None = None,
    height: int | None = None,
) -> FastAPI:
    """Build a node app over a genesis-only chain seeded with known objects.

    ``height`` is accepted for source compatibility with the M2 stub and
    ignored: the new API always reports the chain's real height.
    """
    del height
    chain = Chain.from_genesis_file(
        params, str(default_genesis_path(params.network_id)), params=params
    )
    service = NodeService(
        params=params,
        chain=chain,
        mempool=Mempool(params=params, tip_state=chain.state),
        beacons=dict(beacons or {}),
        transcripts=dict(transcripts or {}),
    )
    return create_node_app(service)


__all__ = ["create_app"]
