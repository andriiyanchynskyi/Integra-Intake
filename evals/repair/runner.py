"""Runner convenience for the deterministic repair contract suite."""

from __future__ import annotations

from collections.abc import Sequence

from app.observability import NULL_OBSERVER, Observer
from app.tenants.config import TenantConfig
from uuid import UUID

from evals.core import ScenarioCase, ScenarioResult, run_scenario_case

from .cases import REPAIR_CASES


async def run_repair_cases(
    tenant_config: TenantConfig,
    *,
    cases: Sequence[ScenarioCase] = REPAIR_CASES,
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


__all__ = ["run_repair_cases"]
