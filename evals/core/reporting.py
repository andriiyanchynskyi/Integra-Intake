"""Source-free reporting helpers for universal scenario results."""

from __future__ import annotations

import json
from collections.abc import Iterable

from .contracts import SAFE_RESULT_KEYS, ScenarioResult


def safe_projection(result: ScenarioResult) -> dict[str, object]:
    """Return the closed projection used by local reports and assertions."""

    projection = result.safe_projection
    if set(projection) != SAFE_RESULT_KEYS:
        raise ValueError("scenario result projection is outside the allowlist")
    return projection


def safe_projections(results: Iterable[ScenarioResult]) -> tuple[dict[str, object], ...]:
    return tuple(safe_projection(result) for result in results)


def render_json(results: Iterable[ScenarioResult]) -> str:
    return json.dumps(
        list(safe_projections(results)),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        indent=2,
    ) + "\n"


__all__ = ["render_json", "safe_projection", "safe_projections"]
