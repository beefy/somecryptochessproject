"""A synchronous in-process transport for ASGI apps.

``httpx.ASGITransport`` is async-only from httpx 0.28 on, while the node, the
relay and the miner clients are synchronous. :class:`ASGITransport` bridges the
two: one HTTP request is driven through the app with ``asyncio.run``, so a test
or ``sccp devnet up`` can call an app object directly without opening a socket.

The transport is deliberately minimal: it supports request/response bodies and
headers, which is everything the node and relay APIs use. Lifespan events are
not started; callers that need shutdown hooks own the app's resources.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from typing import Any

import httpx


class ASGITransport(httpx.BaseTransport):
    """Drive an ASGI app synchronously from an ``httpx.Client``."""

    def __init__(self, app: Any) -> None:
        if not callable(app):
            raise TypeError("app must be an ASGI callable")
        self.app = app

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        """Run one request through the app and return its response."""
        return asyncio.run(self._handle(request))

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        body = request.read()
        scope: dict[str, Any] = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": request.method,
            "scheme": request.url.scheme,
            "path": request.url.path,
            "raw_path": request.url.raw_path.split(b"?", 1)[0],
            "query_string": request.url.query,
            "root_path": "",
            "headers": [(key.lower(), value) for key, value in request.headers.raw],
            "client": ("127.0.0.1", 0),
            "server": (request.url.host, request.url.port or 80),
        }
        sent = False
        finished = asyncio.Event()

        async def receive() -> dict[str, Any]:
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            # A socket server blocks here until the peer hangs up; the request
            # body is already complete. Reporting ``http.disconnect`` at once
            # would be a lie, and Starlette believes it: a ``FileResponse``
            # watches for a disconnect and cancels its body stream, so the
            # caller would see headers with no content. Block until the app has
            # produced its whole response instead.
            await finished.wait()
            return {"type": "http.disconnect"}

        messages: list[dict[str, Any]] = []

        async def send(message: dict[str, Any]) -> None:
            messages.append(message)

        try:
            await self.app(scope, receive, send)
        finally:
            finished.set()
        return self._response(request, messages)

    @staticmethod
    def _response(request: httpx.Request, messages: Iterable[dict[str, Any]]) -> httpx.Response:
        status = 500
        headers: list[tuple[str, str]] = []
        chunks: list[bytes] = []
        for message in messages:
            if message["type"] == "http.response.start":
                status = int(message["status"])
                headers = [
                    (key.decode("latin-1"), value.decode("latin-1"))
                    for key, value in message.get("headers", [])
                ]
            elif message["type"] == "http.response.body":
                chunks.append(message.get("body", b""))
        return httpx.Response(
            status_code=status, headers=headers, content=b"".join(chunks), request=request
        )


def client_for(app: Any, *, base_url: str = "http://inproc") -> httpx.Client:
    """An ``httpx.Client`` that talks to ``app`` in-process."""
    return httpx.Client(transport=ASGITransport(app), base_url=base_url)


__all__ = ["ASGITransport", "client_for"]
