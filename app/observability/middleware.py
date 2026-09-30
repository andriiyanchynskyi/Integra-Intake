"""ASGI request correlation without reading or buffering request bodies."""

from __future__ import annotations

import re
import time
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any
from uuid import uuid4

from starlette.requests import Request

from app.observability.context import ObservationContext
from app.observability.events import (
    Component,
    EventLevel,
    EventName,
    ObservationEvent,
    OutcomeCode,
    RouteName,
)
from app.observability.observer import NULL_OBSERVER, Observer, safe_emit


ASGIMessage = MutableMapping[str, Any]
ASGISend = Callable[[ASGIMessage], Awaitable[None]]
CLIENT_CORRELATION_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def _state(request: Request) -> MutableMapping[str, Any]:
    value = request.scope.setdefault("state", {})
    if not isinstance(value, MutableMapping):
        raise RuntimeError("request state is unavailable")
    return value


def request_observer(request: Request) -> Observer:
    value = _state(request).get("observer")
    if value is not None and hasattr(value, "emit"):
        return value
    application_state = getattr(request.app, "state", None)
    value = getattr(application_state, "observer", None)
    if value is not None and hasattr(value, "emit"):
        return value
    return NULL_OBSERVER


def request_context(request: Request) -> ObservationContext:
    value = _state(request).get("observation_context")
    if not isinstance(value, ObservationContext):
        raise RuntimeError("request observation context is unavailable")
    return value


def bind_request_context(request: Request, context: ObservationContext) -> None:
    _state(request)["observation_context"] = context


def _route_name(scope: MutableMapping[str, Any]) -> RouteName:
    route = scope.get("route")
    template = getattr(route, "path", None)
    if not isinstance(template, str):
        return RouteName.UNKNOWN
    try:
        return RouteName(template)
    except ValueError:
        return RouteName.UNKNOWN


def _header_value(scope: MutableMapping[str, Any], name: bytes) -> str | None:
    for key, value in scope.get("headers", []):
        if key.lower() == name.lower():
            try:
                return value.decode("latin-1")
            except (AttributeError, UnicodeDecodeError):
                return None
    return None


def _request_correlation(value: str | None) -> tuple[str | None, bool]:
    if value is None:
        return None, False
    if CLIENT_CORRELATION_PATTERN.fullmatch(value) is None:
        return None, True
    return value, False


def _safe_content_length(scope: MutableMapping[str, Any]) -> int | None:
    value = _header_value(scope, b"content-length")
    if value is None:
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if parsed >= 0 else None


def _request_outcome(status_code: int) -> tuple[OutcomeCode, EventLevel]:
    if status_code >= 500:
        return OutcomeCode.FAILED, EventLevel.ERROR
    if status_code >= 400:
        return OutcomeCode.REJECTED, EventLevel.WARNING
    return OutcomeCode.SUCCESS, EventLevel.INFO


class ObservabilityMiddleware:
    """Create request IDs and emit one safe completion event per HTTP request."""

    def __init__(
        self,
        app: Callable[..., Awaitable[None]],
        *,
        observer: Observer = NULL_OBSERVER,
    ) -> None:
        self.app = app
        self.observer = observer
        self.clock: Callable[[], int] = time.perf_counter_ns

    async def __call__(
        self,
        scope: MutableMapping[str, Any],
        receive: Callable[[], Awaitable[ASGIMessage]],
        send: ASGISend,
    ) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        request_id = uuid4()
        provisional_trace_id = uuid4()
        client_correlation_id, client_correlation_invalid = _request_correlation(
            _header_value(scope, b"x-correlation-id")
        )
        state = scope.setdefault("state", {})
        context = ObservationContext(
            trace_id=provisional_trace_id,
            request_id=request_id,
        )
        state["observation_context"] = context
        state["observer"] = self._observer()
        state["client_correlation_id"] = client_correlation_id
        state["client_correlation_invalid"] = client_correlation_invalid
        started_ns = self.clock()
        status_code = 500

        async def send_with_trace(message: ASGIMessage) -> None:
            nonlocal status_code
            if message.get("type") == "http.response.start":
                raw_status = message.get("status")
                if isinstance(raw_status, int) and 100 <= raw_status <= 599:
                    status_code = raw_status
                current = state.get("observation_context", context)
                headers = list(message.get("headers", []))
                if not any(key.lower() == b"x-trace-id" for key, _ in headers):
                    headers.append((b"x-trace-id", str(current.trace_id).encode("ascii")))
                message = dict(message)
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_with_trace)
        except Exception:
            status_code = 500
            raise
        finally:
            current = state.get("observation_context", context)
            if not isinstance(current, ObservationContext):
                current = context
            outcome, level = _request_outcome(status_code)
            duration_ms = max(0, (self.clock() - started_ns) // 1_000_000)
            safe_emit(
                state.get("observer", NULL_OBSERVER),
                ObservationEvent(
                    event=EventName.HTTP_REQUEST_COMPLETED,
                    level=level,
                    trace_id=current.trace_id,
                    request_id=current.request_id,
                    tenant_id=current.tenant_id,
                    job_id=current.job_id,
                    case_id=current.case_id,
                    approval_id=current.approval_id,
                    component=Component.API,
                    outcome=outcome,
                    duration_ms=duration_ms,
                    route=_route_name(scope),
                    method=(
                        scope.get("method")
                        if scope.get("method") in {"GET", "POST"}
                        else None
                    ),
                    status_code=status_code,
                    request_bytes=_safe_content_length(scope),
                    client_correlation_id=state.get("client_correlation_id"),
                    client_correlation_invalid=(
                        True if state.get("client_correlation_invalid") else None
                    ),
                    scenario_key=current.scenario_key,
                    profile_fingerprint=current.profile_fingerprint,
                ),
            )

    def _observer(self) -> Observer:
        if hasattr(self.observer, "emit"):
            return self.observer
        return NULL_OBSERVER


__all__ = [
    "CLIENT_CORRELATION_PATTERN",
    "ObservabilityMiddleware",
    "bind_request_context",
    "request_context",
    "request_observer",
]
