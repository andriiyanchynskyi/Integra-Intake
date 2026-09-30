"""Asynchronous intake acceptance boundary."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_tenant
from app.core.config import settings
from app.db.models import Tenant
from app.db.session import get_db_session
from app.domain.intake import (
    CreateIntakeRequest,
    EnqueueIntakeCommand,
    IdempotencyConflict,
    IntakeEnqueueService,
)
from app.policy.models import TrustedSource
from app.observability.middleware import bind_request_context, request_context, request_observer
from app.runtime.profiles import TenantProfileResolver, TenantProfileUnavailableError


router = APIRouter(prefix="/intake", tags=["intake"])


class IntakeAcceptedResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    job_id: UUID
    status: str


@router.post("", response_model=IntakeAcceptedResponse, status_code=status.HTTP_202_ACCEPTED)
async def enqueue_intake(
    request: CreateIntakeRequest,
    http_request: Request,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session, use_cache=False),
) -> IntakeAcceptedResponse:
    if idempotency_key is None or not idempotency_key.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Idempotency-Key is required",
        )
    source = TrustedSource(
        channel=request.channel,
        subject=request.subject,
        body=request.body,
    )
    command = EnqueueIntakeCommand(
        tenant_id=tenant.id,
        tenant_slug=tenant.slug,
        source=source,
        idempotency_key=idempotency_key,
        trace_id=request_context(http_request).trace_id,
    )
    try:
        result = await IntakeEnqueueService(
            session,
            TenantProfileResolver(settings.tenant_profiles_directory),
            observer=request_observer(http_request),
            context=request_context(http_request),
        ).enqueue(command)
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
            detail="Invalid intake request",
        ) from error
    bind_request_context(
        http_request,
        request_context(http_request).bind(
            tenant_id=tenant.id,
            job_id=result.job_id,
            trace_id=result.trace_id,
        ),
    )
    return IntakeAcceptedResponse(job_id=result.job_id, status="queued")
