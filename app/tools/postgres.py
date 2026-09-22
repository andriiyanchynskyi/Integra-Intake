"""Async PostgreSQL implementation behind the synchronous tool-port contract."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent import ToolData
from app.db.models import AgentJob, IdempotencyRecord
from app.policy import TrustedSource
from app.runtime.gateway import WorkerAsyncGateway
from app.tools.models import CreatedCase, CustomerSummary, UpdatedCase
from app.tools.ports import CustomerNotFoundError, TenantToolPort
from app.domain.repositories import CaseRepository
from app.domain.service import CaseService


class PostgresTenantToolPort:
    """Async, job-bound persistence operations with explicit tenant predicates."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        job_id: UUID,
        attempt_count: int | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._job_id = job_id
        self._attempt_count = attempt_count

    async def find_customer(
        self,
        tenant_id: UUID,
        *,
        email: str | None,
        external_id: str | None,
    ) -> CustomerSummary | None:
        async with self._session_factory() as session:
            customer = await CaseRepository(session).find_customer_for_tenant(
                tenant_id,
                email=email,
                external_id=external_id,
            )
            if customer is None:
                return None
            return CustomerSummary(
                id=str(customer.id),
                external_id=customer.external_id,
                name=customer.name,
                email=customer.email,
                phone=customer.phone,
            )

    async def case_exists(self, tenant_id: UUID, case_id: UUID) -> bool:
        async with self._session_factory() as session:
            case = await CaseRepository(session).get_for_tenant(case_id, tenant_id)
            return case is not None

    async def create_case(
        self,
        tenant_id: UUID,
        source: TrustedSource,
        *,
        customer_id: UUID | None,
        fields: Mapping[str, ToolData],
    ) -> CreatedCase:
        async with self._session_factory() as session:
            async with session.begin():
                repository = CaseRepository(session)
                idempotency = await self._lock_idempotency(session, tenant_id)
                if idempotency.case_id is not None:
                    existing = await repository.get_for_tenant(
                        idempotency.case_id, tenant_id
                    )
                    if existing is None:
                        raise RuntimeError("job case is unavailable")
                    return CreatedCase(
                        id=str(existing.id),
                        status=existing.status,
                        accepted_fields=sorted(existing.extracted_fields),
                    )
                if customer_id is not None:
                    customer = await repository.get_customer_for_tenant(
                        customer_id, tenant_id
                    )
                    if customer is None:
                        raise CustomerNotFoundError
                case = await CaseService(session).create_agent_case(
                    tenant_id,
                    source,
                    customer_id=customer_id,
                    fields=fields,
                )
                await session.flush()
                CaseService(session).append_event(
                    tenant_id,
                    case.id,
                    event_type="created",
                    payload={},
                )
                idempotency.case_id = case.id
                await self._mark_side_effect(session, tenant_id)
                return CreatedCase(
                    id=str(case.id),
                    status=case.status,
                    accepted_fields=sorted(fields),
                )

    async def update_case_fields(
        self,
        tenant_id: UUID,
        case_id: UUID,
        *,
        fields: Mapping[str, ToolData],
    ) -> UpdatedCase | None:
        async with self._session_factory() as session:
            async with session.begin():
                repository = CaseRepository(session)
                case = await repository.get_for_tenant_for_update(case_id, tenant_id)
                if case is None:
                    return None
                merged = deepcopy(dict(case.extracted_fields or {}))
                merged.update(deepcopy(dict(fields)))
                case.extracted_fields = merged
                CaseService(session).append_event(
                    tenant_id,
                    case.id,
                    event_type="fields_updated",
                    payload={"updated_fields": sorted(fields)},
                )
                await self._mark_side_effect(session, tenant_id)
                return UpdatedCase(
                    id=str(case.id),
                    updated_fields=sorted(fields),
                )

    async def _lock_idempotency(
        self, session: AsyncSession, tenant_id: UUID
    ) -> IdempotencyRecord:
        statement = (
            select(IdempotencyRecord)
            .where(
                IdempotencyRecord.tenant_id == tenant_id,
                IdempotencyRecord.job_id == self._job_id,
            )
            .with_for_update()
        )
        record = (await session.execute(statement)).scalar_one_or_none()
        if record is None:
            raise RuntimeError("job idempotency record is unavailable")
        return record

    async def _mark_side_effect(self, session: AsyncSession, tenant_id: UUID) -> None:
        conditions = [
            AgentJob.id == self._job_id,
            AgentJob.tenant_id == tenant_id,
        ]
        if self._attempt_count is not None:
            conditions.extend(
                [
                    AgentJob.status == "running",
                    AgentJob.attempt_count == self._attempt_count,
                ]
            )
        statement = select(AgentJob).where(*conditions).with_for_update()
        job = (await session.execute(statement)).scalar_one_or_none()
        if job is None:
            raise RuntimeError("agent job is unavailable")
        job.side_effect_committed_at = datetime.now(timezone.utc)


class SyncTenantToolPort(TenantToolPort):
    """Synchronous facade used only by the agent thread."""

    def __init__(
        self,
        gateway: WorkerAsyncGateway,
        async_port: PostgresTenantToolPort,
    ) -> None:
        self._gateway = gateway
        self._async_port = async_port

    def find_customer(
        self,
        tenant_id: UUID,
        *,
        email: str | None,
        external_id: str | None,
    ) -> CustomerSummary | None:
        return self._gateway.call(
            self._async_port.find_customer(
                tenant_id, email=email, external_id=external_id
            )
        )

    def create_case(
        self,
        tenant_id: UUID,
        source: TrustedSource,
        *,
        customer_id: UUID | None,
        fields: Mapping[str, ToolData],
    ) -> CreatedCase:
        return self._gateway.call(
            self._async_port.create_case(
                tenant_id,
                source,
                customer_id=customer_id,
                fields=fields,
            )
        )

    def update_case_fields(
        self,
        tenant_id: UUID,
        case_id: UUID,
        *,
        fields: Mapping[str, ToolData],
    ) -> UpdatedCase | None:
        return self._gateway.call(
            self._async_port.update_case_fields(
                tenant_id,
                case_id,
                fields=fields,
            )
        )

    def case_exists(self, tenant_id: UUID, case_id: UUID) -> bool:
        return self._gateway.call(
            self._async_port.case_exists(tenant_id, case_id)
        )


__all__ = ["PostgresTenantToolPort", "SyncTenantToolPort"]
