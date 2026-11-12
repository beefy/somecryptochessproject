"""Read-only node API stubs (section 5.12).

M2 has no chain yet, but the relay and the future node must speak the same
models. These routes expose exactly what exists -- network parameters, a known
epoch beacon and stored transcripts -- and nothing that pretends a chain is
running beyond a ``height`` field.
"""

from __future__ import annotations

from collections.abc import Mapping

from fastapi import FastAPI, HTTPException

from sccp.config import NetworkParams
from sccp.protocol.models import Beacon, Transcript
from sccp.version import PROTOCOL_VERSION, __version__


def create_app(
    params: NetworkParams,
    *,
    beacons: Mapping[int, Beacon] | None = None,
    transcripts: Mapping[str, Transcript] | None = None,
    height: int | None = None,
) -> FastAPI:
    """Build the read-only node stub for ``params``."""
    known_beacons = dict(beacons or {})
    known_transcripts = dict(transcripts or {})
    app = FastAPI(title="sccp node (M2 stub)", version=__version__)

    @app.get("/v1/status")
    def status() -> dict[str, object]:
        return {
            "network": params.network_id,
            "protocol_version": PROTOCOL_VERSION,
            "height": height,
            "tip_hash": None,
            "peers": 0,
            "mempool": {"transactions": 0, "proofs": 0},
        }

    @app.get("/v1/params")
    def network_params() -> dict[str, object]:
        return params.model_dump(mode="json")

    @app.get("/v1/epochs/{epoch}/beacon", response_model=Beacon)
    def epoch_beacon(epoch: int) -> Beacon:
        try:
            return known_beacons[epoch]
        except KeyError:
            raise HTTPException(
                status_code=404, detail=f"no beacon known for epoch {epoch}"
            ) from None

    @app.get("/v1/matches/{match_id}", response_model=Transcript)
    def match_transcript(match_id: str) -> Transcript:
        try:
            return known_transcripts[match_id]
        except KeyError:
            raise HTTPException(status_code=404, detail=f"unknown match {match_id}") from None

    return app


__all__ = ["create_app"]
