"""ASGI hardening: early request-body limits and security headers on every response."""

from __future__ import annotations

import json
from typing import Any

Scope = dict[str, Any]
Message = dict[str, Any]
Receive = Any
Send = Any
ASGIApp = Any

SECURITY_HEADERS: tuple[tuple[bytes, bytes], ...] = (
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (b"cache-control", b"no-store"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
    (b"cross-origin-opener-policy", b"same-origin"),
)
# The API serves JSON only; the interactive docs (development) load their own assets.
API_CSP = b"default-src 'none'; frame-ancestors 'none'; base-uri 'none'"
DOCS_CSP = b"frame-ancestors 'none'; object-src 'none'; base-uri 'self'"
DOCS_PATHS = ("/docs", "/redoc")


class _BodyTooLarge(Exception):
    pass


async def _reject(send: Send, status: int, detail: str) -> None:
    body = json.dumps({"detail": detail}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class RequestHardening:
    """Reject oversized bodies before any parsing and add security headers."""

    def __init__(self, app: ASGIApp, max_body_bytes: int) -> None:
        self.app, self.max_body_bytes = app, max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = False
        too_large = False
        csp = DOCS_CSP if str(scope.get("path", "")).startswith(DOCS_PATHS) else API_CSP

        async def send_with_headers(message: Message) -> None:
            nonlocal started
            if too_large:
                # Frameworks may turn the aborted read into their own error; answer 413 instead.
                if message["type"] == "http.response.start" and not started:
                    started = True
                    await _reject(send_with_headers_raw, 413, "request body too large")
                return
            await send_with_headers_raw(message)

        async def send_with_headers_raw(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                present = {name.lower() for name, _ in message.get("headers", [])}
                extra = [(n, v) for n, v in SECURITY_HEADERS if n not in present]
                if b"content-security-policy" not in present:
                    extra.append((b"content-security-policy", csp))
                message = {**message, "headers": [*message.get("headers", []), *extra]}
            await send(message)

        length = dict(scope.get("headers", [])).get(b"content-length")
        if length is not None:
            if not length.isdigit():
                await _reject(send_with_headers_raw, 400, "invalid content length")
                return
            if int(length) > self.max_body_bytes:
                await _reject(send_with_headers_raw, 413, "request body too large")
                return
        received = 0

        async def limited_receive() -> Message:
            nonlocal received, too_large
            message: Message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_body_bytes:
                    too_large = True
                    raise _BodyTooLarge
            return dict(message)

        try:
            await self.app(scope, limited_receive, send_with_headers)
        except _BodyTooLarge:
            if not started:
                started = True
                await _reject(send_with_headers_raw, 413, "request body too large")
