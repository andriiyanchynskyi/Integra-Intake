"""Async persistence operations for intake jobs and their idempotency keys."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AgentJob, Approval, IdempotencyRecord
from app.domain.jobs import PersistedJobRead
from app.policy.models import TrustedSource
from app.runtime.profiles import ResolvedTenantProfile

if TYPE_CHECKING:
    from app.domain.intake import EnqueueIntakeCommand


@dataclass(frozen=True, slots=True)
class ClaimedJob:
    id: UUID
    tenant_id: UUID
    trace_id: UUID
    status: str
    source_snapshot: dict[str, object]
    tenant_config_snapshot: dict[str, object]
    tenant_config_sha256: str
    risk_signals: dict[str, object]
    attempt_count: int
    side_effect_committed_at: datetime | None
    created_at: datetime
    available_at: datetime
    started_at: datetime


@dataclass(frozen=True, slots=True)
class JobObservationIdentity:
    """Immutable profile identity used when an idempotent job is reused."""

    trace_id: UUID
    tenant_config_snapshot: dict[str, object]
    tenant_config_sha256: str


@dataclass(frozen=True, slots=True)
class LeaseRecoveryObservation:
    """Closed lifecycle data returned when a running lease is recovered."""

    trace_id: UUID
    tenant_id: UUID
    job_id: UUID
    attempt_count: int
    status: str
    error_code: str
    tenant_config_snapshot: dict[str, object] = field(default_factory=dict)
    tenant_config_sha256: str | None = None


class JobLeaseLostError(RuntimeError):
    """The worker no longer owns the running job attempt."""


class JobRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_idempotency_for_tenant(
        self,
        tenant_id: UUID,
        key: str,
        *,
        for_update: bool = False,
    ) -> IdempotencyRecord | None:
        statement = select(IdempotencyRecord).where(
            IdempotencyRecord.tenant_id == tenant_id,
            IdempotencyRecord.key == key,
        )
        if for_update:
            statement = statement.with_for_update()
        return (await self.session.execute(statement)).scalar_one_or_none()

    async def create_job(
        self,
        command: EnqueueIntakeCommand,
        profile: ResolvedTenantProfile,
    ) -> AgentJob:
        job = AgentJob(
            tenant_id=command.tenant_id,
            trace_id=command.trace_id,
            source_snapshot=self._source_snapshot(command.source),
            tenant_config_snapshot=deepcopy(profile.snapshot),
            tenant_config_sha256=profile.sha256,
            risk_signals={
                "safety_or_legal_risk": command.risk_signals.safety_or_legal_risk,
            },
        )
        self.session.add(job)
        return job

    async def get_trace_id(
        self, job_id: UUID, *, tenant_id: UUID
    ) -> UUID | None:
        statement = select(AgentJob.trace_id).where(
            AgentJob.id == job_id,
            AgentJob.tenant_id == tenant_id,
        )
        return (await self.session.execute(statement)).scalar_one_or_none()

    async def get_read_for_tenant(
        self,
        job_id: UUID,
        tenant_id: UUID,
    ) -> PersistedJobRead | None:
        """Read one job and its approval identity under the tenant predicate."""

        statement = (
            select(AgentJob, Approval.id, Approval.status)
            .outerjoin(
                Approval,
                (Approval.job_id == AgentJob.id)
                & (Approval.tenant_id == AgentJob.tenant_id),
            )
            .where(AgentJob.id == job_id, AgentJob.tenant_id == tenant_id)
        )
        row = (await self.session.execute(statement)).one_or_none()
        if row is None:
            return None
        job, approval_id, approval_status = row
        if (
            not isinstance(job, AgentJob)
            or not isinstance(job.source_snapshot, dict)
            or not isinstance(job.tenant_config_snapshot, dict)
            or not isinstance(job.tenant_config_sha256, str)
            or not isinstance(job.created_at, datetime)
        ):
            return None
        return PersistedJobRead(
            job_id=job.id,
            trace_id=job.trace_id,
            status=job.status,
            attempt_count=job.attempt_count,
            result=deepcopy(job.result) if isinstance(job.result, dict) else None,
            error_code=job.error_code,
            tenant_config_snapshot=deepcopy(job.tenant_config_snapshot),
            tenant_config_sha256=job.tenant_config_sha256,
            approval_id=approval_id if isinstance(approval_id, UUID) else None,
            approval_status=approval_status if isinstance(approval_status, str) else None,
            created_at=job.created_at,
            started_at=job.started_at,
            finished_at=job.finished_at,
        )

    async def get_observation_identity(
        self, job_id: UUID, *, tenant_id: UUID
    ) -> JobObservationIdentity | None:
        statement = select(
            AgentJob.trace_id,
            AgentJob.tenant_config_snapshot,
            AgentJob.tenant_config_sha256,
        ).where(
            AgentJob.id == job_id,
            AgentJob.tenant_id == tenant_id,
        )
        row = (await self.session.execute(statement)).one_or_none()
        if row is None:
            return None
        trace_id, snapshot, sha256 = row
        if not isinstance(snapshot, dict) or not isinstance(sha256, str):
            return None
        return JobObservationIdentity(
            trace_id=trace_id,
            tenant_config_snapshot=deepcopy(snapshot),
            tenant_config_sha256=sha256,
        )

    @staticmethod
    def _source_snapshot(source: TrustedSource) -> dict[str, object]:
        snapshot: dict[str, object] = {
            "channel": source.channel,
            "subject": source.subject,
            "body": source.body,
        }
        if source.sender is not None:
            snapshot["sender"] = source.sender
        if source.document is not None:
            snapshot["document"] = source.document.model_dump(mode="json")
        return snapshot

    async def create_idempotency_record(
        self,
        tenant_id: UUID,
        key: str,
        request_hash: str,
        job_id: UUID,
    ) -> IdempotencyRecord:
        record = IdempotencyRecord(
            tenant_id=tenant_id,
            key=key,
            request_hash=request_hash,
            job_id=job_id,
            case_id=None,
            response={},
        )
        self.session.add(record)
        return record

    async def recover_expired_leases(
        self,
        *,
        now: datetime,
        max_retries: int,
        retry_delay: timedelta,
    ) -> tuple[LeaseRecoveryObservation, ...]:
        async with self.session.begin():
            statement = (
                select(AgentJob)
                .where(
                    AgentJob.status == "running",
                    AgentJob.lease_expires_at.is_not(None),
                    AgentJob.lease_expires_at <= now,
                )
                .with_for_update(skip_locked=True)
            )
            jobs = (await self.session.execute(statement)).scalars().all()
            recovered: list[LeaseRecoveryObservation] = []
            for job in jobs:
                job.lease_expires_at = None
                if job.side_effect_committed_at is not None:
                    job.status = "failed_uncertain"
                    job.error_code = "lease_expired_after_side_effect"
                    job.finished_at = now
                elif job.attempt_count <= max_retries:
                    job.status = "queued"
                    job.available_at = now + retry_delay
                    job.error_code = "lease_expired"
                else:
                    job.status = "failed"
                    job.error_code = "retry_exhausted"
                    job.finished_at = now
                recovered.append(
                    LeaseRecoveryObservation(
                        trace_id=job.trace_id,
                        tenant_id=job.tenant_id,
                        job_id=job.id,
                        attempt_count=job.attempt_count,
                        status=job.status,
                        error_code=job.error_code or "worker_error",
                        tenant_config_snapshot=deepcopy(job.tenant_config_snapshot),
                        tenant_config_sha256=job.tenant_config_sha256,
                    )
                )
            return tuple(recovered)

    async def claim_next(
        self,
        *,
        now: datetime,
        lease_until: datetime,
    ) -> ClaimedJob | None:
        async with self.session.begin():
            statement = (
                select(AgentJob)
                .where(
                    AgentJob.status == "queued",
                    AgentJob.available_at <= now,
                )
                .order_by(AgentJob.available_at, AgentJob.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            job = (await self.session.execute(statement)).scalar_one_or_none()
            if job is None:
                return None
            job.status = "running"
            job.attempt_count += 1
            job.started_at = job.started_at or now
            job.lease_expires_at = lease_until
            await self.session.flush()
            return ClaimedJob(
                id=job.id,
                tenant_id=job.tenant_id,
                trace_id=job.trace_id,
                status=job.status,
                source_snapshot=deepcopy(job.source_snapshot),
                tenant_config_snapshot=deepcopy(job.tenant_config_snapshot),
                tenant_config_sha256=job.tenant_config_sha256,
                risk_signals=deepcopy(job.risk_signals),
                attempt_count=job.attempt_count,
                side_effect_committed_at=job.side_effect_committed_at,
                created_at=job.created_at,
                available_at=job.available_at,
                started_at=job.started_at,
            )

    async def renew_lease(
        self,
        job_id: UUID,
        *,
        tenant_id: UUID,
        attempt_count: int,
        lease_until: datetime,
    ) -> bool:
        """Extend a lease only when this worker still owns its attempt."""

        async with self.session.begin():
            job = await self._get_job(
                job_id,
                tenant_id,
                for_update=True,
                attempt_count=attempt_count,
            )
            if job is None or job.status != "running":
                return False
            job.lease_expires_at = lease_until
            return True

    async def mark_succeeded(
        self,
        job_id: UUID,
        result: dict[str, object],
        *,
        now: datetime,
        tenant_id: UUID,
        attempt_count: int,
    ) -> None:
        await self._mark_terminal(
            job_id,
            status="succeeded",
            now=now,
            tenant_id=tenant_id,
            attempt_count=attempt_count,
            result=result,
            error_code=None,
        )

    async def mark_failed(
        self,
        job_id: UUID,
        error_code: str,
        *,
        now: datetime,
        tenant_id: UUID,
        attempt_count: int,
    ) -> None:
        await self._mark_terminal(
            job_id,
            status="failed",
            now=now,
            tenant_id=tenant_id,
            attempt_count=attempt_count,
            result=None,
            error_code=error_code,
        )

    async def mark_failed_uncertain(
        self,
        job_id: UUID,
        error_code: str,
        *,
        now: datetime,
        tenant_id: UUID,
        attempt_count: int,
    ) -> None:
        await self._mark_terminal(
            job_id,
            status="failed_uncertain",
            now=now,
            tenant_id=tenant_id,
            attempt_count=attempt_count,
            result=None,
            error_code=error_code,
        )

    async def schedule_retry(
        self,
        job_id: UUID,
        error_code: str,
        *,
        available_at: datetime,
        tenant_id: UUID,
        attempt_count: int,
    ) -> None:
        async with self.session.begin():
            job = await self._get_job(
                job_id,
                tenant_id,
                for_update=True,
                attempt_count=attempt_count,
            )
            if job is None or job.status != "running":
                raise JobLeaseLostError(
                    "job lease ownership changed before retry scheduling"
                )
            job.status = "queued"
            job.error_code = error_code
            job.available_at = available_at
            job.lease_expires_at = None

    async def get_side_effect_marker(
        self,
        job_id: UUID,
        *,
        tenant_id: UUID,
        attempt_count: int,
    ) -> datetime | None:
        """Return the durable mutation marker for a tenant-owned job.

        Workers use this read after an exception that happened outside the
        database thread.  The marker is deliberately the only execution
        state consulted for deciding whether replay is safe; no transcript or
        provider output is reconstructed here.
        """

        job = await self._get_job(
            job_id,
            tenant_id,
            for_update=False,
            attempt_count=attempt_count,
        )
        if job is None or job.status != "running":
            raise JobLeaseLostError(
                "job lease ownership changed before side-effect inspection"
            )
        return job.side_effect_committed_at

    async def _mark_terminal(
        self,
        job_id: UUID,
        *,
        status: str,
        now: datetime,
        tenant_id: UUID,
        attempt_count: int,
        result: dict[str, object] | None,
        error_code: str | None,
    ) -> None:
        async with self.session.begin():
            job = await self._get_job(
                job_id,
                tenant_id,
                for_update=True,
                attempt_count=attempt_count,
            )
            if job is None or job.status != "running":
                raise JobLeaseLostError(
                    "job lease ownership changed before terminal transition"
                )
            job.status = status
            job.finished_at = now
            job.lease_expires_at = None
            job.result = deepcopy(result) if result is not None else None
            job.error_code = error_code

    async def _get_job(
        self,
        job_id: UUID,
        tenant_id: UUID,
        *,
        for_update: bool,
        attempt_count: int,
    ) -> AgentJob | None:
        statement = select(AgentJob).where(
            AgentJob.id == job_id,
            AgentJob.tenant_id == tenant_id,
            AgentJob.attempt_count == attempt_count,
        )
        if for_update:
            statement = statement.with_for_update()
        return (await self.session.execute(statement)).scalar_one_or_none()
