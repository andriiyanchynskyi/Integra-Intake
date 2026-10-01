"""Tenant-scoped read-only job status and result endpoint."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_tenant
from app.db.models import Tenant
from app.db.session import get_db_session
from app.domain.job_repository import JobRepository
from app.domain.jobs import JobRead, project_job_read
from app.runtime.profiles import TenantProfileUnavailableError


router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.get("/{job_id}", response_model=JobRead)
async def get_job(
    job_id: UUID,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session, use_cache=False),
) -> JobRead:
    row = await JobRepository(session).get_read_for_tenant(job_id, tenant.id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Job not found",
        )
    try:
        return project_job_read(row)
    except (TenantProfileUnavailableError, RuntimeError, TypeError, ValueError) as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Job profile unavailable",
        ) from error
