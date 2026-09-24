"""Authenticated email-like inbound webhook acceptance boundary."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_inbound_tenant, read_bounded_inbound_body
from app.core.config import settings
from app.db.models import Tenant
from app.db.session import get_db_session
from app.domain.intake import IdempotencyConflict
from app.domain.intake import IntakeEnqueueService
from app.inbound import (
    InboundIntakeService,
    InvalidInboundPayload,
    parse_webhook_email_payload,
)
from app.runtime.profiles import TenantProfileResolver, TenantProfileUnavailableError

from app.api.intake import IntakeAcceptedResponse


router = APIRouter(prefix="/inbound/email", tags=["inbound"])


def get_inbound_intake_service(
    session: Annotated[AsyncSession, Depends(get_db_session, use_cache=False)],
) -> InboundIntakeService:
    return InboundIntakeService(
        IntakeEnqueueService(
            session,
            TenantProfileResolver(settings.tenant_profiles_directory),
        )
    )


@router.post(
    "/webhook",
    response_model=IntakeAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def receive_email_webhook(
    request: Request,
    tenant: Annotated[Tenant, Depends(get_current_inbound_tenant)],
    service: Annotated[InboundIntakeService, Depends(get_inbound_intake_service)],
) -> IntakeAcceptedResponse:
    if (
        request.headers.get("content-type", "").split(";", 1)[0].lower()
        != "application/json"
    ):
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Unsupported media type",
        )

    raw_body = await read_bounded_inbound_body(request)
    try:
        payload = parse_webhook_email_payload(raw_body)
        result = await service.enqueue(payload.to_message(tenant.id, tenant.slug))
    except InvalidInboundPayload as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Invalid inbound payload",
        ) from error
    except IdempotencyConflict as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Idempotency-Key conflicts with request",
        ) from error
    except TenantProfileUnavailableError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Tenant profile unavailable",
        ) from error
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Invalid inbound payload",
        ) from error
    return IntakeAcceptedResponse(job_id=result.job_id, status="queued")


__all__ = ["get_inbound_intake_service", "router"]
