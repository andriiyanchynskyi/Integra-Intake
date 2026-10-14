"""Tenant-scoped retrieval of durable draft and review artifacts."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_tenant
from app.db.models import Tenant
from app.db.session import get_db_session
from app.domain.artifact_repository import ArtifactRepository
from app.domain.artifacts import ArtifactRead, project_artifact


router = APIRouter(prefix="/artifacts", tags=["artifacts"])


@router.get("/{artifact_id}", response_model=ArtifactRead)
async def get_artifact(
    artifact_id: UUID,
    tenant: Annotated[Tenant, Depends(get_current_tenant)],
    session: Annotated[AsyncSession, Depends(get_db_session, use_cache=False)],
) -> ArtifactRead:
    artifact = await ArtifactRepository(session).get_for_tenant(
        artifact_id,
        tenant.id,
    )
    if artifact is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Artifact not found",
        )
    try:
        return project_artifact(artifact)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Artifact not found",
        ) from None


__all__ = ["router"]
