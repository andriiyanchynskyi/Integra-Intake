"""Runner convenience for the deterministic language-school suite."""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from app.observability import NULL_OBSERVER, Observer
from app.tenants.config import TenantConfig

from evals.core import ScenarioCase, ScenarioResult, run_scenario_case

from .cases import EDUCATION_CASES


async def run_education_cases(
    tenant_config: TenantConfig,
    *,
    cases: Sequence[ScenarioCase] = EDUCATION_CASES,
    tenant_id: UUID | None = None,
    observer: Observer = NULL_OBSERVER,
) -> tuple[ScenarioResult, ...]:
    return tuple(
        await run_scenario_case(
            case,
            tenant_config=tenant_config,
            tenant_id=tenant_id,
            observer=observer,
        )
        for case in cases
    )


__all__ = ["run_education_cases"]
