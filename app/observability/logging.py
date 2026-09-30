"""Idempotent local JSON logging configuration."""

from __future__ import annotations

import sys
import threading

import structlog


_CONFIGURATION_LOCK = threading.Lock()
_CONFIGURED = False


class _DynamicPrintLoggerFactory:
    """Resolve stdout when a logger is built, including under captured tests."""

    def __call__(self, *args: object, **kwargs: object) -> structlog.PrintLogger:
        del args, kwargs
        return structlog.PrintLogger(file=sys.stdout)


def configure_json_logging() -> None:
    """Configure deterministic UTC JSON output once per process."""

    global _CONFIGURED
    if _CONFIGURED:
        return
    with _CONFIGURATION_LOCK:
        if _CONFIGURED:
            return
        structlog.configure(
            processors=[
                structlog.processors.TimeStamper(fmt="iso", utc=True),
                structlog.processors.add_log_level,
                structlog.processors.JSONRenderer(sort_keys=True),
            ],
            logger_factory=_DynamicPrintLoggerFactory(),
            cache_logger_on_first_use=False,
        )
        _CONFIGURED = True


__all__ = ["configure_json_logging"]
