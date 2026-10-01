"""Reject oversized HTTP request bodies before framework parsing."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

from starlette.responses import JSONResponse

from app.inbound.models import MAX_INBOUND_WEBHOOK_BODY_BYTES


MAX_API_REQUEST_BODY_BYTES = 1024 * 1024
ASGIMessage = MutableMapping[str, Any]
ASGIReceive = Callable[[], Awaitable[ASGIMessage]]
ASGISend = Callable[[ASGIMessage], Awaitable[None]]


class _RequestBodyTooLarge(Exception):
    pass


def _header_value(scope: MutableMapping[str, Any], name: bytes) -> str | None:
    for key, value in scope.get("headers", []):
        if key.lower() == name:
            try:
                return value.decode("latin-1")
            except (AttributeError, UnicodeDecodeError):
                return None
    return None


def _declared_content_length(scope: MutableMapping[str, Any]) -> int | None:
    value = _header_value(scope, b"content-length")
    if value is None:
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if parsed >= 0 else None


def _request_limit(scope: MutableMapping[str, Any]) -> int | None:
    if scope.get("type") != "http" or scope.get("method") != "POST":
        return None
    if scope.get("path") == "/v1/inbound/email/webhook":
        return MAX_INBOUND_WEBHOOK_BODY_BYTES
    return MAX_API_REQUEST_BODY_BYTES


class RequestBodyLimitMiddleware:
    """Apply a bounded streaming limit to POST request bodies."""

    def __init__(self, app: Callable[..., Awaitable[None]]) -> None:
        self.app = app

    async def __call__(
        self,
        scope: MutableMapping[str, Any],
        receive: ASGIReceive,
        send: ASGISend,
    ) -> None:
        limit = _request_limit(scope)
        if limit is None:
            await self.app(scope, receive, send)
            return

        declared = _declared_content_length(scope)
        if declared is not None and declared > limit:
            await self._too_large(scope, receive, send)
            return

        total = 0

        async def limited_receive() -> ASGIMessage:
            nonlocal total
            message = await receive()
            if message.get("type") == "http.request":
                body = message.get("body", b"")
                if isinstance(body, (bytes, bytearray, memoryview)):
                    total += len(body)
                if total > limit:
                    raise _RequestBodyTooLarge
            return message

        try:
            await self.app(scope, limited_receive, send)
        except _RequestBodyTooLarge:
            await self._too_large(scope, receive, send)

    @staticmethod
    async def _too_large(
        scope: MutableMapping[str, Any],
        receive: ASGIReceive,
        send: ASGISend,
    ) -> None:
        detail = (
            "Inbound webhook body too large"
            if scope.get("path") == "/v1/inbound/email/webhook"
            else "Request body too large"
        )
        await JSONResponse(
            {"detail": detail},
            status_code=413,
        )(scope, receive, send)


__all__ = [
    "MAX_API_REQUEST_BODY_BYTES",
    "RequestBodyLimitMiddleware",
    "_request_limit",
]
