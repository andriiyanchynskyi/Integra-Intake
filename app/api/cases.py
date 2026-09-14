from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_tenant
from app.db.models import IntakeCase, Tenant
from app.db.session import get_db_session
from app.domain.repositories import CaseRepository
from app.domain.schemas import CreateCaseRequest
from app.domain.service import CaseService


router = APIRouter(prefix="/cases", tags=["cases"])


class CaseResponse(BaseModel):
    id: UUID
    customer_id: UUID | None
    status: str
    channel: str
    subject: str
    body: str
    source: str | None
    raw_payload: dict
    extracted_fields: dict
    created_at: datetime


def serialize_case(case: IntakeCase) -> CaseResponse:
    return CaseResponse(
        id=case.id,
        customer_id=case.customer_id,
        status=case.status,
        channel=case.channel,
        subject=case.subject,
        body=case.body,
        source=case.source,
        raw_payload=case.raw_payload,
        extracted_fields=case.extracted_fields,
        created_at=case.created_at,
    )


@router.post("", response_model=CaseResponse, status_code=status.HTTP_201_CREATED)
async def create_case(
    request: CreateCaseRequest,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session, use_cache=False),
) -> CaseResponse:
    case = await CaseService(session).create_case(tenant.id, request)
    return serialize_case(case)


@router.get("/{case_id}", response_model=CaseResponse)
async def get_case(
    case_id: UUID,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session, use_cache=False),
) -> CaseResponse:
    case = await CaseRepository(session).get_for_tenant(case_id, tenant.id)
    if case is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Case not found")
    return serialize_case(case)
