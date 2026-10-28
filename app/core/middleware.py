"""Pure-ASGI middleware: request IDs + access log, security headers, body size limit."""

from __future__ import annotations

import json
import logging
import re
import time
import uuid

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.logging_config import request_id_ctx

access_logger = logging.getLogger("app.access")

_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{8,64}$")
_DOC_PATHS = ("/docs", "/redoc", "/openapi.json")


def _set_header(
    headers: list[tuple[bytes, bytes]],
    name: bytes,
    value: bytes,
    *,
    force: bool = False,
) -> None:
    for index, (existing, _) in enumerate(headers):
        if existing.lower() == name:
            if force:
                headers[index] = (name, value)
            return
    headers.append((name, value))


class RequestContextMiddleware:
    """Assign/propagate X-Request-ID and emit one access-log line per request."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = dict(scope["headers"]).get(b"x-request-id", b"").decode("latin-1")
        request_id = incoming if _VALID_REQUEST_ID.match(incoming) else uuid.uuid4().hex
        token = request_id_ctx.set(request_id)
        started = time.perf_counter()
        status_code = 500

        async def send_with_id(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                headers = list(message.get("headers", []))
                _set_header(headers, b"x-request-id", request_id.encode(), force=True)
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            # Path only: query strings can carry identifiers and are never logged.
            access_logger.info(
                "request",
                extra={
                    "method": scope["method"],
                    "path": scope["path"],
                    "status": status_code,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                },
            )
            request_id_ctx.reset(token)


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp, *, hsts: bool = False) -> None:
        self.app = app
        self.hsts = hsts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        is_docs = scope["path"].startswith(_DOC_PATHS)

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                _set_header(headers, b"x-content-type-options", b"nosniff")
                _set_header(headers, b"x-frame-options", b"DENY")
                _set_header(headers, b"referrer-policy", b"no-referrer")
                _set_header(
                    headers,
                    b"permissions-policy",
                    b"geolocation=(), microphone=(), camera=()",
                )
                if not is_docs:
                    _set_header(
                        headers,
                        b"content-security-policy",
                        b"default-src 'none'; frame-ancestors 'none'",
                    )
                    _set_header(headers, b"cache-control", b"no-store")
                if self.hsts:
                    _set_header(
                        headers,
                        b"strict-transport-security",
                        b"max-age=31536000; includeSubDomains",
                    )
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_headers)


class BodySizeLimitMiddleware:
    """Reject request bodies larger than ``max_bytes`` with 413."""

    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        declared = dict(scope["headers"]).get(b"content-length", b"")
        if declared.isdigit() and int(declared) > self.max_bytes:
            await self._reject(send)
            return

        received = 0
        too_large = False
        responded = False

        async def limited_receive() -> Message:
            nonlocal received, too_large
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    too_large = True
                    # Stop feeding the app; whatever it replies is swapped for a 413.
                    return {"type": "http.request", "body": b"", "more_body": False}
            return message

        async def guarded_send(message: Message) -> None:
            nonlocal responded
            if too_large:
                if not responded:
                    responded = True
                    await self._reject(send)
                return
            await send(message)

        await self.app(scope, limited_receive, guarded_send)

    @staticmethod
    async def _reject(send: Send) -> None:
        body = json.dumps({"detail": "Request body too large"}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
