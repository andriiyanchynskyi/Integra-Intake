from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import CaseEvent, IntakeCase
from app.domain.schemas import CreateCaseRequest


class CaseRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, tenant_id: UUID, request: CreateCaseRequest) -> IntakeCase:
        case = IntakeCase(
            tenant_id=tenant_id,
            customer_id=request.customer_id,
            status="received",
            channel=request.channel,
            subject=request.subject,
            body=request.body,
            source=request.channel,
            raw_payload={
                "channel": request.channel,
                "subject": request.subject,
                "body": request.body,
            },
            extracted_fields=request.extracted_fields,
        )
        self.session.add(case)
        return case

    async def create_created_event(self, tenant_id: UUID, case_id: UUID) -> CaseEvent:
        event = CaseEvent(
            tenant_id=tenant_id,
            case_id=case_id,
            event_type="created",
            actor=None,
            payload={},
        )
        self.session.add(event)
        return event

    async def get_for_tenant(self, case_id: UUID, tenant_id: UUID) -> IntakeCase | None:
        statement = select(IntakeCase).where(
            IntakeCase.id == case_id,
            IntakeCase.tenant_id == tenant_id,
        )
        return (await self.session.execute(statement)).scalar_one_or_none()
