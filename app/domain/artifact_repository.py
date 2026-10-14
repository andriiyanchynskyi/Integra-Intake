"""Tenant-scoped persistence for operational job artifacts."""

from __future__ import annotations

from copy import deepcopy
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent import ResultReference
from app.db.models import JobArtifact
from app.domain.artifacts import ArtifactKind


class ArtifactRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_for_tenant(
        self,
        artifact_id: UUID,
        tenant_id: UUID,
    ) -> JobArtifact | None:
        statement = select(JobArtifact).where(
            JobArtifact.id == artifact_id,
            JobArtifact.tenant_id == tenant_id,
        )
        return (await self.session.execute(statement)).scalar_one_or_none()

    async def create_for_job(
        self,
        tenant_id: UUID,
        job_id: UUID,
        *,
        kind: ArtifactKind,
        payload: dict[str, object],
    ) -> ResultReference:
        statement = (
            select(JobArtifact)
            .where(
                JobArtifact.tenant_id == tenant_id,
                JobArtifact.job_id == job_id,
                JobArtifact.kind == kind,
            )
            .with_for_update()
        )
        existing = (await self.session.execute(statement)).scalar_one_or_none()
        if existing is not None:
            return ResultReference(kind=kind, id=existing.id)
        artifact = JobArtifact(
            tenant_id=tenant_id,
            job_id=job_id,
            kind=kind,
            payload=deepcopy(payload),
        )
        try:
            async with self.session.begin_nested():
                self.session.add(artifact)
                await self.session.flush()
        except IntegrityError:
            # Two attempts for one job/kind may race before either sees the
            # unique row. The savepoint keeps the caller's transaction usable;
            # the committed artifact is the authoritative replay result.
            existing = (
                await self.session.execute(
                    select(JobArtifact).where(
                        JobArtifact.tenant_id == tenant_id,
                        JobArtifact.job_id == job_id,
                        JobArtifact.kind == kind,
                    )
                )
            ).scalar_one_or_none()
            if existing is None:
                raise
            return ResultReference(kind=kind, id=existing.id)
        return ResultReference(kind=kind, id=artifact.id)


__all__ = ["ArtifactRepository"]
