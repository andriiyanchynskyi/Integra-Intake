"""Tenant-scoped persistence for approval requests and lifecycle events."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AgentJob, Approval, ApprovalEvent
from app.tools.models import ApprovalRequested, PendingAction


class ApprovalCreationConflict(RuntimeError):
    """The claimed job is no longer able to create an approval."""


@dataclass(frozen=True, slots=True)
class ExpiredApprovalObservation:
    """Safe correlation data for an approval expired by the worker."""

    trace_id: UUID
    tenant_id: UUID
    job_id: UUID
    approval_id: UUID
    created_at: datetime
    decided_at: datetime
    tenant_config_snapshot: dict[str, object] = field(default_factory=dict)
    tenant_config_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class ApprovalObservationIdentity:
    """Stored job identity used for later approval observations."""

    trace_id: UUID
    tenant_config_snapshot: dict[str, object]
    tenant_config_sha256: str


class ApprovalRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create_for_running_job(
        self,
        tenant_id: UUID,
        job_id: UUID,
        *,
        attempt_count: int,
        action: PendingAction,
        policy_reason: str,
        expires_in: timedelta,
        now: datetime,
    ) -> ApprovalRequested:
        """Create one approval and park its running job atomically."""

        async with self.session.begin():
            job_statement = (
                select(AgentJob)
                .where(
                    AgentJob.id == job_id,
                    AgentJob.tenant_id == tenant_id,
                )
                .with_for_update()
            )
            job = (await self.session.execute(job_statement)).scalar_one_or_none()
            if job is None:
                raise ApprovalCreationConflict("job is unavailable")

            existing_statement = (
                select(Approval)
                .where(
                    Approval.tenant_id == tenant_id,
                    Approval.job_id == job_id,
                )
                .with_for_update()
            )
            existing = (
                await self.session.execute(existing_statement)
            ).scalar_one_or_none()
            if existing is not None:
                if existing.pending_action is None:
                    raise ApprovalCreationConflict("legacy approval cannot be reused")
                if existing.expires_at is None:
                    raise ApprovalCreationConflict("approval expiry is unavailable")
                return ApprovalRequested(
                    id=existing.id,
                    expires_at=existing.expires_at,
                )

            if job.status != "running" or job.attempt_count != attempt_count:
                raise ApprovalCreationConflict("job is not running")

            expires_at = now + expires_in
            approval = Approval(
                tenant_id=tenant_id,
                job_id=job_id,
                case_id=None,
                action=action.name,
                status="pending",
                decision={},
                policy_reason=policy_reason,
                pending_action=action.model_dump(mode="json"),
                tenant_config_sha256=job.tenant_config_sha256,
                expires_at=expires_at,
                execution_result={},
            )
            self.session.add(approval)
            await self.session.flush()
            self._add_event(
                tenant_id,
                approval.id,
                event_type="approval_requested",
                actor_ref=None,
                payload={
                    "action": action.name,
                    "policy_reason": policy_reason,
                },
            )
            job.status = "awaiting_approval"
            job.finished_at = now
            job.lease_expires_at = None
            job.result = {
                "status": "awaiting_approval",
                "approval_id": str(approval.id),
                "policy_reason": policy_reason,
            }
            job.error_code = None
            return ApprovalRequested(id=approval.id, expires_at=expires_at)

    async def get_for_tenant_for_update(
        self, approval_id: UUID, tenant_id: UUID
    ) -> Approval | None:
        statement = (
            select(Approval)
            .where(
                Approval.id == approval_id,
                Approval.tenant_id == tenant_id,
            )
            .with_for_update()
        )
        return (await self.session.execute(statement)).scalar_one_or_none()

    async def get_job_trace_id(self, approval: Approval) -> UUID:
        if approval.job_id is None:
            raise ApprovalCreationConflict("approval job is unavailable")
        statement = select(AgentJob.trace_id).where(
            AgentJob.id == approval.job_id,
            AgentJob.tenant_id == approval.tenant_id,
        )
        trace_id = (await self.session.execute(statement)).scalar_one_or_none()
        if trace_id is None:
            raise ApprovalCreationConflict("approval job is unavailable")
        return trace_id

    async def expire_due(
        self, *, now: datetime, limit: int = 100
    ) -> tuple[ExpiredApprovalObservation, ...]:
        async with self.session.begin():
            statement = (
                select(Approval)
                .where(
                    Approval.status == "pending",
                    Approval.expires_at.is_not(None),
                    Approval.expires_at <= now,
                )
                .order_by(Approval.expires_at, Approval.id)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
            approvals = (await self.session.execute(statement)).scalars().all()
            observations: list[ExpiredApprovalObservation] = []
            for approval in approvals:
                self.expire_locked(approval, now=now)
                if approval.job_id is None or approval.created_at is None:
                    continue
                identity = await self.get_job_observation_identity(approval)
                if identity is None:
                    continue
                observations.append(
                    ExpiredApprovalObservation(
                        trace_id=identity.trace_id,
                        tenant_id=approval.tenant_id,
                        job_id=approval.job_id,
                        approval_id=approval.id,
                        created_at=approval.created_at,
                        decided_at=now,
                        tenant_config_snapshot=deepcopy(
                            identity.tenant_config_snapshot
                        )
                        if identity is not None
                        else {},
                        tenant_config_sha256=(
                            identity.tenant_config_sha256
                            if identity is not None
                            else None
                        ),
                    )
                )
            return tuple(observations)

    async def _trace_id_for_approval(self, approval: Approval) -> UUID | None:
        if approval.job_id is None:
            return None
        statement = select(AgentJob.trace_id).where(
            AgentJob.id == approval.job_id,
            AgentJob.tenant_id == approval.tenant_id,
        )
        return (await self.session.execute(statement)).scalar_one_or_none()

    async def get_job_observation_identity(
        self, approval: Approval
    ) -> ApprovalObservationIdentity | None:
        if approval.job_id is None:
            return None
        statement = select(
            AgentJob.trace_id,
            AgentJob.tenant_config_snapshot,
            AgentJob.tenant_config_sha256,
        ).where(
            AgentJob.id == approval.job_id,
            AgentJob.tenant_id == approval.tenant_id,
        )
        row = (await self.session.execute(statement)).one_or_none()
        if row is None:
            return None
        trace_id, snapshot, sha256 = row
        if not isinstance(snapshot, dict) or not isinstance(sha256, str):
            return None
        return ApprovalObservationIdentity(
            trace_id=trace_id,
            tenant_config_snapshot=deepcopy(snapshot),
            tenant_config_sha256=sha256,
        )

    def expire_locked(self, approval: Approval, *, now: datetime) -> None:
        """Transition one already-locked pending row to timeout rejection."""

        if approval.status != "pending":
            return
        approval.status = "expired"
        approval.decided_at = now
        approval.decided_by_actor_ref = "system_timeout"
        approval.decision_reason = "approval_timeout"
        approval.decision = {
            "decision": "reject",
            "reason": "approval_timeout",
        }
        self.add_event(
            approval.tenant_id,
            approval.id,
            event_type="approval_expired",
            actor_ref="system_timeout",
            payload={},
        )

    def add_event(
        self,
        tenant_id: UUID,
        approval_id: UUID,
        *,
        event_type: str,
        actor_ref: str | None,
        payload: dict[str, object],
    ) -> ApprovalEvent:
        return self._add_event(
            tenant_id,
            approval_id,
            event_type=event_type,
            actor_ref=actor_ref,
            payload=payload,
        )

    def _add_event(
        self,
        tenant_id: UUID,
        approval_id: UUID,
        *,
        event_type: str,
        actor_ref: str | None,
        payload: dict[str, object],
    ) -> ApprovalEvent:
        event = ApprovalEvent(
            tenant_id=tenant_id,
            approval_id=approval_id,
            event_type=event_type,
            actor_ref=actor_ref,
            payload=payload,
        )
        self.session.add(event)
        return event


__all__ = [
    "ApprovalCreationConflict",
    "ApprovalObservationIdentity",
    "ApprovalRepository",
    "ExpiredApprovalObservation",
]
