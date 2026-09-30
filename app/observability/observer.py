"""Fail-open observer boundary and structured JSON adapter."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Protocol

import structlog

from app.observability.events import EventLevel, ObservationEvent


class Observer(Protocol):
    def emit(self, event: ObservationEvent) -> None:
        """Consume one already-validated event."""


class NullObserver:
    """No-op observer used by existing callers and tests."""

    def emit(self, event: ObservationEvent) -> None:
        del event


NULL_OBSERVER: Observer = NullObserver()


class RecordingObserver:
    """Small deterministic sink useful at repository seams and in evals."""

    def __init__(self) -> None:
        self.events: list[ObservationEvent] = []

    def emit(self, event: ObservationEvent) -> None:
        self.events.append(event)


class StructlogObserver:
    """Render only the closed event model through the configured logger."""

    def __init__(self, logger: object | None = None) -> None:
        self._logger = logger or structlog.get_logger("integra.observability")

    def emit(self, event: ObservationEvent) -> None:
        payload = event.model_dump(mode="json", exclude_none=True)
        payload.pop("event", None)
        level = payload.pop("level", EventLevel.INFO.value)
        log_method: Callable[..., object] = getattr(self._logger, str(level))
        log_method(event.event.value, **payload)


def safe_emit(observer: Observer, event: ObservationEvent) -> None:
    """Emit without allowing logging/serialization failures to affect intake."""

    try:
        observer.emit(event)
    except Exception:
        logging.getLogger("app.observability").error(
            "observability_emit_failed",
            exc_info=False,
        )


__all__ = [
    "NULL_OBSERVER",
    "NullObserver",
    "Observer",
    "RecordingObserver",
    "StructlogObserver",
    "safe_emit",
]
