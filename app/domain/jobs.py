"""Tenant-scoped, source-free projections for durable agent jobs."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.agent.models import StopReason
from app.observability.events import OutcomeCode, PolicyReason, WorkerErrorCode
from app.runtime.profiles import resolve_persisted_profile
from app.tenants.config import RoutingDecision, RoutingStatus
from app.tenants.identifiers import SafeIdentifier


MAX_EXECUTION_STEPS = 8


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


class ExecutionPathRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    step: int
    action_key: SafeIdentifier | None
    action_known: bool
    policy_decision: RoutingDecision
    routing_status: RoutingStatus
    policy_reason: PolicyReason
    missing_required_fields: tuple[SafeIdentifier, ...] = ()
    tool_outcome: OutcomeCode | None = None
    side_effect_committed: bool


class JobRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    job_id: UUID
    trace_id: UUID
    scenario_key: str
    status: JobStatus
    attempt_count: int
    agent_steps: int | None = None
    stop_reason: StopReason | None = None
    execution_path: tuple[ExecutionPathRead, ...] = ()
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


def _safe_execution_values(
    result: Mapping[str, object] | None,
    registered_actions: frozenset[str],
) -> tuple[int | None, StopReason | None, tuple[ExecutionPathRead, ...]]:
    if result is None or not ("steps" in result or "execution_path" in result):
        return None, None, ()

    steps = result.get("steps")
    agent_steps = (
        steps
        if isinstance(steps, int)
        and not isinstance(steps, bool)
        and 0 <= steps <= MAX_EXECUTION_STEPS
        else None
    )
    stop_reason = None
    raw_reason = result.get("reason")
    if isinstance(raw_reason, str):
        try:
            stop_reason = StopReason(raw_reason)
        except ValueError:
            pass

    raw_path = result.get("execution_path")
    if raw_path is None:
        return agent_steps, stop_reason, ()
    if not isinstance(raw_path, list) or len(raw_path) > MAX_EXECUTION_STEPS:
        return agent_steps, stop_reason, ()

    path: list[ExecutionPathRead] = []
    previous_step = 0
    expected_keys = {
        "step",
        "action_key",
        "action_known",
        "policy_decision",
        "routing_status",
        "policy_reason",
        "missing_required_fields",
        "tool_outcome",
        "side_effect_committed",
    }
    for item in raw_path:
        if not isinstance(item, Mapping) or set(item) != expected_keys:
            return agent_steps, stop_reason, ()
        step = item.get("step")
        action_known = item.get("action_known")
        action_key = item.get("action_key")
        missing = item.get("missing_required_fields")
        if (
            not isinstance(step, int)
            or isinstance(step, bool)
            or not 1 <= step <= MAX_EXECUTION_STEPS
            or step <= previous_step
            or not isinstance(action_known, bool)
            or (action_known and not isinstance(action_key, str))
            or (not action_known and action_key is not None)
            or (action_known and action_key not in registered_actions)
            or not isinstance(missing, list)
            or not all(isinstance(value, str) for value in missing)
            or not isinstance(item.get("side_effect_committed"), bool)
        ):
            return agent_steps, stop_reason, ()
        try:
            path.append(
                ExecutionPathRead(
                    step=step,
                    action_key=action_key,
                    action_known=action_known,
                    policy_decision=RoutingDecision(item["policy_decision"]),
                    routing_status=RoutingStatus(item["routing_status"]),
                    policy_reason=PolicyReason(item["policy_reason"]),
                    missing_required_fields=tuple(sorted(set(missing))),
                    tool_outcome=(
                        None
                        if item["tool_outcome"] is None
                        else OutcomeCode(item["tool_outcome"])
                    ),
                    side_effect_committed=item["side_effect_committed"],
                )
            )
        except (TypeError, ValueError):
            return agent_steps, stop_reason, ()
        previous_step = step
    return agent_steps, stop_reason, tuple(path)


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
    registered_actions = (
        profile.compiled.registered_actions
        if profile.compiled is not None
        else frozenset(profile.config.action_policy)
    )
    agent_steps, stop_reason, execution_path = _safe_execution_values(
        row.result,
        registered_actions,
    )
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
        agent_steps=agent_steps,
        stop_reason=stop_reason,
        execution_path=execution_path,
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
    "ExecutionPathRead",
    "JobRead",
    "JobStatus",
    "PersistedJobRead",
    "project_job_read",
]
