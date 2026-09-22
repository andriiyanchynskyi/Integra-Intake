"""Bounded retry policy for explicitly safe external HTTP operations."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar


T = TypeVar("T")


class RetryableHttpError(RuntimeError):
    """An operation may be retried when its request is idempotent."""


TransientHttpError = RetryableHttpError


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_retries: int = 4
    delays: tuple[float, ...] = (0.5, 1.0, 2.0, 4.0)

    def __post_init__(self) -> None:
        if self.max_retries < 0:
            raise ValueError("max_retries must be non-negative")
        if len(self.delays) < self.max_retries:
            raise ValueError("delays must cover every retry")
        if any(delay < 0 for delay in self.delays):
            raise ValueError("retry delays must be non-negative")


def run_http_with_retry(
    operation: Callable[[], T],
    *,
    method: str,
    idempotency_key: str | None = None,
    policy: RetryPolicy | None = None,
    sleep: Callable[[float], None],
    random_uniform: Callable[[float, float], float],
) -> T:
    """Run a declared-safe operation, preserving its original exception."""

    retry_policy = policy or RetryPolicy()
    normalized_method = method.upper()
    safe = normalized_method == "GET" or (
        normalized_method == "POST" and bool(idempotency_key and idempotency_key.strip())
    )
    retries = 0
    while True:
        try:
            return operation()
        except RetryableHttpError:
            if not safe or retries >= retry_policy.max_retries:
                raise
            delay = retry_policy.delays[retries]
            sleep(delay + random_uniform(0.0, delay * 0.1))
            retries += 1
