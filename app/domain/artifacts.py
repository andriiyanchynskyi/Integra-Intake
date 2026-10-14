"""Typed, tenant-scoped operational artifact contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, Union
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.db.models import JobArtifact


ArtifactKind = Literal["reply_draft", "review_flag"]


class ReplyDraftPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    recipient: str | None
    subject: str
    body: str


class ReviewFlagPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    reason: str


class ReplyDraftRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    kind: Literal["reply_draft"]
    id: UUID
    job_id: UUID
    recipient: str | None
    subject: str
    body: str
    created_at: datetime


class ReviewFlagRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    kind: Literal["review_flag"]
    id: UUID
    job_id: UUID
    reason: str
    created_at: datetime


ArtifactRead = Annotated[
    Union[ReplyDraftRead, ReviewFlagRead],
    Field(discriminator="kind"),
]


def project_artifact(row: JobArtifact) -> ArtifactRead:
    """Validate one stored payload before returning it to its tenant."""

    if row.kind == "reply_draft":
        payload = ReplyDraftPayload.model_validate(row.payload)
        return ReplyDraftRead(
            kind="reply_draft",
            id=row.id,
            job_id=row.job_id,
            recipient=payload.recipient,
            subject=payload.subject,
            body=payload.body,
            created_at=row.created_at,
        )
    if row.kind == "review_flag":
        payload = ReviewFlagPayload.model_validate(row.payload)
        return ReviewFlagRead(
            kind="review_flag",
            id=row.id,
            job_id=row.job_id,
            reason=payload.reason,
            created_at=row.created_at,
        )
    raise ValueError("unsupported artifact kind")


__all__ = [
    "ArtifactKind",
    "ArtifactRead",
    "ReplyDraftPayload",
    "ReplyDraftRead",
    "ReviewFlagPayload",
    "ReviewFlagRead",
    "project_artifact",
]
