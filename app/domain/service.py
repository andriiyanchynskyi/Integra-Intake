from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import IntakeCase
from app.domain.repositories import CaseRepository
from app.domain.schemas import CreateCaseRequest


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
