"""The relay's browser allowlist (``sccp matchmaker run --origin``).

A browser seat talks to the relay directly, so the relay needs CORS headers for
the explorer's origin -- and for nothing else. These tests pin both halves: an
allowed origin is answered, an unknown one is not, and a relay started without
``--origin`` never grows CORS headers at all.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from sccp.miner.matchmaker import Matchmaker, create_app

ORIGIN = "http://127.0.0.1:8645"


def _state(signed_match) -> Matchmaker:
    """A relay over the fixture match's beacon and roster."""
    return Matchmaker(
        signed_match.params,
        beacon=signed_match.beacon,
        roster=signed_match.roster,
        public_keys=signed_match.public_keys,
        engines={
            account.address: f"mock-v1:{index:016d}"
            for index, account in enumerate(signed_match.accounts.values(), start=1)
        },
        book=signed_match.book,
    )


def test_preflight_is_answered_for_an_allowed_origin(signed_match) -> None:
    client = TestClient(create_app(_state(signed_match), origins=(ORIGIN,)))

    response = client.options(
        "/v1/matches",
        headers={
            "origin": ORIGIN,
            "access-control-request-method": "POST",
            "access-control-request-headers": "content-type",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == ORIGIN
    assert "POST" in response.headers["access-control-allow-methods"]
    assert "content-type" in response.headers["access-control-allow-headers"]


def test_a_read_carries_the_allow_origin_header(signed_match) -> None:
    client = TestClient(create_app(_state(signed_match), origins=(ORIGIN,)))

    response = client.get("/v1/status", headers={"origin": ORIGIN})

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == ORIGIN


def test_another_origin_gets_no_grant(signed_match) -> None:
    client = TestClient(create_app(_state(signed_match), origins=(ORIGIN,)))

    response = client.get("/v1/status", headers={"origin": "https://elsewhere.example"})

    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers


def test_no_allowlist_means_no_cors_headers(signed_match) -> None:
    client = TestClient(create_app(_state(signed_match)))

    response = client.get("/v1/status", headers={"origin": ORIGIN})

    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers
