from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import CaseEvent, Customer, IntakeCase
from app.domain.schemas import CreateCaseRequest
from app.policy.models import TrustedSource
from app.agent import ToolData


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

    async def get_for_tenant_for_update(
        self, case_id: UUID, tenant_id: UUID
    ) -> IntakeCase | None:
        statement = (
            select(IntakeCase)
            .where(
                IntakeCase.id == case_id,
                IntakeCase.tenant_id == tenant_id,
            )
            .with_for_update()
        )
        return (await self.session.execute(statement)).scalar_one_or_none()

    async def get_customer_for_tenant(
        self, customer_id: UUID, tenant_id: UUID
    ) -> Customer | None:
        statement = select(Customer).where(
            Customer.id == customer_id,
            Customer.tenant_id == tenant_id,
        )
        return (await self.session.execute(statement)).scalar_one_or_none()

    async def find_customer_for_tenant(
        self,
        tenant_id: UUID,
        *,
        email: str | None,
        external_id: str | None,
    ) -> Customer | None:
        identifiers = []
        if email is not None:
            identifiers.append(Customer.email == email)
        if external_id is not None:
            identifiers.append(Customer.external_id == external_id)
        if not identifiers:
            return None
        statement = select(Customer).where(
            Customer.tenant_id == tenant_id,
            or_(*identifiers),
        )
        return (await self.session.execute(statement)).scalars().first()

    async def create_from_source(
        self,
        tenant_id: UUID,
        source: TrustedSource,
        *,
        customer_id: UUID | None,
        fields: dict[str, ToolData],
    ) -> IntakeCase:
        case = IntakeCase(
            tenant_id=tenant_id,
            customer_id=customer_id,
            status="received",
            channel=source.channel,
            subject=source.subject,
            body=source.body,
            source=source.channel,
            raw_payload={
                "channel": source.channel,
                "subject": source.subject,
                "body": source.body,
            },
            extracted_fields=fields,
        )
        self.session.add(case)
        return case

    def add_event(
        self,
        tenant_id: UUID,
        case_id: UUID,
        *,
        event_type: str,
        payload: dict,
    ) -> CaseEvent:
        event = CaseEvent(
            tenant_id=tenant_id,
            case_id=case_id,
            event_type=event_type,
            actor=None,
            payload=payload,
        )
        self.session.add(event)
        return event
