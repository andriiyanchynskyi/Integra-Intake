from collections.abc import Mapping
from copy import deepcopy
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import IntakeCase
from app.domain.repositories import CaseRepository
from app.domain.schemas import CreateCaseRequest
from app.policy.models import TrustedSource
from app.agent import ToolData


class CaseService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repository = CaseRepository(session)

    async def create_case(self, tenant_id: UUID, request: CreateCaseRequest) -> IntakeCase:
        async with self.session.begin():
            case = await self.repository.create(tenant_id, request)
            await self.session.flush()
            await self.repository.create_created_event(tenant_id, case.id)
        return case

    async def create_agent_case(
        self,
        tenant_id: UUID,
        source: TrustedSource,
        *,
        customer_id: UUID | None,
        fields: Mapping[str, ToolData],
    ) -> IntakeCase:
        return await self.repository.create_from_source(
            tenant_id,
            source,
            customer_id=customer_id,
            fields=deepcopy(dict(fields)),
        )

    def append_event(
        self,
        tenant_id: UUID,
        case_id: UUID,
        *,
        event_type: str,
        payload: dict,
    ) -> None:
        self.repository.add_event(
            tenant_id,
            case_id,
            event_type=event_type,
            payload=payload,
        )
