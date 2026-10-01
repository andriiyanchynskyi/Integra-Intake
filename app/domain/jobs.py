"""Tenant-scoped, source-free projections for durable agent jobs."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.observability.events import WorkerErrorCode
from app.runtime.profiles import resolve_persisted_profile


JobStatus = Literal[
    "queued",
    "running",
    "awaiting_approval",
    "succeeded",
    "failed",
    "failed_uncertain",
]
_JOB_STATUSES = frozenset(
    {"queued", "running", "awaiting_approval", "succeeded", "failed", "failed_uncertain"}
)


@dataclass(frozen=True, slots=True)
class PersistedJobRead:
    """Internal read projection; snapshots never cross the API boundary."""

    job_id: UUID
    trace_id: UUID
    status: str
    attempt_count: int
    result: dict[str, object] | None
    error_code: str | None
    tenant_config_snapshot: dict[str, object]
    tenant_config_sha256: str
    approval_id: UUID | None
    approval_status: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class ApprovalRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    approval_id: UUID
    status: str


class JobRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    job_id: UUID
    trace_id: UUID
    scenario_key: str
    status: JobStatus
    attempt_count: int
    routing_status: str | None = None
    routing_reason: str | None = None
    missing_required_fields: tuple[str, ...] = ()
    approval: ApprovalRead | None = None
    error_code: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


def _safe_error_code(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return WorkerErrorCode(value).value
    except ValueError:
        return None


def _safe_result_values(
    result: Mapping[str, object] | None,
) -> tuple[str | None, str | None, tuple[str, ...]]:
    if result is None:
        return None, None, ()
    routing_status = result.get("routing_status")
    routing_reason = result.get("routing_reason")
    missing = result.get("missing_required_fields")
    safe_missing: tuple[str, ...] = ()
    if isinstance(missing, (list, tuple)) and all(isinstance(item, str) for item in missing):
        safe_missing = tuple(sorted(set(missing)))
    return (
        routing_status if isinstance(routing_status, str) else None,
        routing_reason if isinstance(routing_reason, str) else None,
        safe_missing,
    )


def project_job_read(row: PersistedJobRead) -> JobRead:
    """Resolve trusted identity and return only the closed public read model."""

    if row.status not in _JOB_STATUSES:
        raise ValueError("unsupported job status")
    profile = resolve_persisted_profile(
        row.tenant_config_snapshot,
        row.tenant_config_sha256,
    )
    scenario_key = (
        profile.compiled.scenario_key
        if profile.compiled is not None
        else profile.config.scenario_key
    )
    routing_status, routing_reason, missing = _safe_result_values(row.result)
    approval = None
    if row.approval_id is not None and isinstance(row.approval_status, str):
        approval = ApprovalRead(
            approval_id=row.approval_id,
            status=row.approval_status,
        )
    return JobRead(
        job_id=row.job_id,
        trace_id=row.trace_id,
        scenario_key=scenario_key,
        status=row.status,
        attempt_count=row.attempt_count,
        routing_status=routing_status,
        routing_reason=routing_reason,
        missing_required_fields=missing,
        approval=approval,
        error_code=_safe_error_code(row.error_code),
        created_at=row.created_at,
        started_at=row.started_at,
        finished_at=row.finished_at,
    )


__all__ = [
    "ApprovalRead",
    "JobRead",
    "JobStatus",
    "PersistedJobRead",
    "project_job_read",
]
