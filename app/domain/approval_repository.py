"""Tenant-scoped persistence for approval requests and lifecycle events."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

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
        """Create one approval for the worker-owned running attempt."""

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

            if job.status != "running" or job.attempt_count != attempt_count:
                raise ApprovalCreationConflict("job is not running")

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
                if existing.status != "staged":
                    raise ApprovalCreationConflict("approval is already published")
                pending_action = action.model_dump(mode="json")
                refreshed_expires_at = now + expires_in
                if (
                    existing.action != action.name
                    or existing.pending_action != pending_action
                    or existing.policy_reason != policy_reason
                    or existing.tenant_config_sha256 != job.tenant_config_sha256
                ):
                    existing.action = action.name
                    existing.policy_reason = policy_reason
                    existing.pending_action = pending_action
                    existing.tenant_config_sha256 = job.tenant_config_sha256
                    self._add_event(
                        tenant_id,
                        existing.id,
                        event_type="approval_replaced",
                        actor_ref=None,
                        payload={
                            "action": action.name,
                            "policy_reason": policy_reason,
                        },
                    )
                existing.expires_at = refreshed_expires_at
                return ApprovalRequested(
                    id=existing.id,
                    expires_at=existing.expires_at,
                )

            expires_at = now + expires_in
            approval = Approval(
                tenant_id=tenant_id,
                job_id=job_id,
                case_id=None,
                action=action.name,
                status="staged",
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
            return ApprovalRequested(id=approval.id, expires_at=expires_at)

    async def get_job_for_update(
        self,
        tenant_id: UUID,
        job_id: UUID,
    ) -> AgentJob | None:
        """Lock the approval's tenant-owned job after the approval row lock."""

        statement = (
            select(AgentJob)
            .where(
                AgentJob.id == job_id,
                AgentJob.tenant_id == tenant_id,
            )
            .with_for_update()
        )
        return (await self.session.execute(statement)).scalar_one_or_none()

    async def get_for_tenant_for_update(
        self, approval_id: UUID, tenant_id: UUID
    ) -> Approval | None:
        statement = (
            select(Approval)
            .outerjoin(
                AgentJob,
                (AgentJob.id == Approval.job_id)
                & (AgentJob.tenant_id == Approval.tenant_id),
            )
            .where(
                Approval.id == approval_id,
                Approval.tenant_id == tenant_id,
                self._published_lifecycle_predicate(),
            )
            .with_for_update(of=Approval)
        )
        return (await self.session.execute(statement)).scalar_one_or_none()

    async def get_for_tenant(
        self, approval_id: UUID, tenant_id: UUID
    ) -> Approval | None:
        """Read one approval through the authenticated tenant predicate."""

        statement = (
            select(Approval)
            .outerjoin(
                AgentJob,
                (AgentJob.id == Approval.job_id)
                & (AgentJob.tenant_id == Approval.tenant_id),
            )
            .where(
                Approval.id == approval_id,
                Approval.tenant_id == tenant_id,
                self._published_lifecycle_predicate(),
            )
        )
        return (await self.session.execute(statement)).scalar_one_or_none()

    async def list_for_tenant(
        self,
        tenant_id: UUID,
        *,
        status: str,
        limit: int,
    ) -> tuple[Approval, ...]:
        """Return one bounded, tenant-scoped approval review page."""

        bounded_limit = min(max(limit, 1), 100)
        statement = (
            select(Approval)
            .outerjoin(
                AgentJob,
                (AgentJob.id == Approval.job_id)
                & (AgentJob.tenant_id == Approval.tenant_id),
            )
            .where(
                Approval.tenant_id == tenant_id,
                Approval.status == status,
                self._published_lifecycle_predicate(),
            )
            .order_by(Approval.expires_at, Approval.id)
            .limit(bounded_limit + 1)
        )
        approvals = (await self.session.execute(statement)).scalars().all()
        return tuple(approvals)

    @staticmethod
    def _published_lifecycle_predicate() -> ColumnElement[bool]:
        return or_(
            and_(
                Approval.job_id.is_(None),
                Approval.status.in_(("pending", "approved", "rejected", "expired")),
            ),
            and_(Approval.status == "pending", AgentJob.status == "awaiting_approval"),
            and_(
                Approval.status.in_(("approved", "rejected")),
                AgentJob.status == "succeeded",
            ),
            and_(Approval.status == "expired", AgentJob.status == "failed"),
        )

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
                .outerjoin(
                    AgentJob,
                    (AgentJob.id == Approval.job_id)
                    & (AgentJob.tenant_id == Approval.tenant_id),
                )
                .where(
                    Approval.status == "pending",
                    or_(
                        Approval.job_id.is_(None),
                        AgentJob.status == "awaiting_approval",
                    ),
                    Approval.expires_at.is_not(None),
                    Approval.expires_at <= now,
                )
                .order_by(Approval.expires_at, Approval.id)
                .limit(limit)
                .with_for_update(of=Approval, skip_locked=True)
            )
            approvals = (await self.session.execute(statement)).scalars().all()
            observations: list[ExpiredApprovalObservation] = []
            for approval in approvals:
                job = None
                if approval.job_id is not None:
                    job = await self.get_job_for_update(
                        approval.tenant_id,
                        approval.job_id,
                    )
                    if job is None:
                        raise ApprovalCreationConflict("approval job is unavailable")
                self.expire_locked(approval, now=now)
                if job is not None:
                    self.finalize_job_for_decision(approval, job, now=now)
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

    def finalize_job_for_decision(
        self,
        approval: Approval,
        job: AgentJob,
        *,
        now: datetime,
    ) -> None:
        """Resolve the already-locked parked job exactly once.

        Approval decisions are the durable boundary for parked jobs.  The
        result intentionally contains only lifecycle metadata and allowlisted
        identifiers; executor payloads never become a job-readable result.
        """

        if approval.status == "approved":
            status = "succeeded"
            error_code = None
            business_outcome = "tool_result"
        elif approval.status == "rejected":
            status = "succeeded"
            error_code = None
            business_outcome = "approval_rejected"
        elif approval.status == "expired":
            status = "failed"
            error_code = "approval_expired"
            business_outcome = "approval_expired"
        else:
            raise ApprovalCreationConflict("approval decision is not final")

        if job.status != "awaiting_approval":
            # A caller may defensively retry after a committed decision.  Do
            # not rewrite a terminal job or clear a side-effect marker.
            if (
                job.status == status
                and isinstance(job.result, dict)
                and job.result.get("approval_id") == str(approval.id)
                and job.result.get("approval_status") == approval.status
            ):
                return
            raise ApprovalCreationConflict("approval job is not awaiting approval")

        result_reference: dict[str, str] = {}
        case_id = approval.case_id
        if case_id is None and isinstance(approval.execution_result, dict):
            raw_case_id = approval.execution_result.get("case_id")
            if isinstance(raw_case_id, str):
                try:
                    case_id = UUID(raw_case_id)
                except ValueError:
                    case_id = None
        if isinstance(case_id, UUID):
            result_reference["case_id"] = str(case_id)

        preserved_result: dict[str, object] = {}
        if isinstance(job.result, dict):
            for key in (
                "reason",
                "steps",
                "execution_path",
                "routing_status",
                "routing_reason",
                "missing_required_fields",
            ):
                if key in job.result:
                    preserved_result[key] = deepcopy(job.result[key])

        job.status = status
        job.error_code = error_code
        job.finished_at = now
        job.lease_expires_at = None
        job.result = {
            **preserved_result,
            "business_outcome": business_outcome,
            "approval_id": str(approval.id),
            "approval_status": approval.status,
            "result_reference": result_reference,
        }

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
