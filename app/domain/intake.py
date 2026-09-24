"""Closed inbound intake contract and idempotent job enqueue service."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import IdempotencyRecord
from app.documents import DocumentNormalizer, RateConfirmationDocumentInput
from app.policy.models import RiskSignals, TrustedSource
from app.domain.job_repository import JobRepository
from app.runtime.profiles import TenantProfileResolver


class CreateIntakeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    channel: str = Field(min_length=1, max_length=100)
    subject: str = Field(min_length=1, max_length=500)
    body: str = Field(min_length=1)

    @field_validator("channel", "subject", "body")
    @classmethod
    def require_non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must not be blank")
        return value


@dataclass(frozen=True, slots=True)
class EnqueueIntakeCommand:
    tenant_id: UUID
    tenant_slug: str
    source: TrustedSource
    idempotency_key: str
    risk_signals: RiskSignals = field(default_factory=RiskSignals)


@dataclass(frozen=True, slots=True)
class EnqueueDocumentIntakeCommand:
    tenant_id: UUID
    tenant_slug: str
    document: RateConfirmationDocumentInput
    idempotency_key: str
    risk_signals: RiskSignals = field(default_factory=RiskSignals)


@dataclass(frozen=True, slots=True)
class EnqueueResult:
    job_id: UUID
    created: bool


class IdempotencyConflict(ValueError):
    """An idempotency key was reused with a different request body."""


def canonical_intake_hash(source: TrustedSource) -> str:
    payload = {
        "body": source.body,
        "channel": source.channel,
        "subject": source.subject,
    }
    if source.document is not None:
        payload["document"] = source.document.model_dump(mode="json")
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class IntakeEnqueueService:
    def __init__(
        self,
        session: AsyncSession,
        profile_resolver: TenantProfileResolver,
        document_normalizer: DocumentNormalizer | None = None,
    ) -> None:
        self.session = session
        self.repository = JobRepository(session)
        self.profile_resolver = profile_resolver
        self.document_normalizer = document_normalizer or DocumentNormalizer()

    async def enqueue(self, command: EnqueueIntakeCommand) -> EnqueueResult:
        return await self._enqueue(command)

    async def enqueue_document(
        self, command: EnqueueDocumentIntakeCommand
    ) -> EnqueueResult:
        normalized = self.document_normalizer.normalize(command.document)
        source = TrustedSource(
            channel=command.document.channel,
            subject=command.document.subject,
            body="Rate confirmation document received.",
            document=normalized,
        )
        return await self._enqueue(
            EnqueueIntakeCommand(
                tenant_id=command.tenant_id,
                tenant_slug=command.tenant_slug,
                source=source,
                idempotency_key=command.idempotency_key,
                risk_signals=command.risk_signals,
            )
        )

    async def _enqueue(self, command: EnqueueIntakeCommand) -> EnqueueResult:
        key = command.idempotency_key.strip()
        if not key or len(key) > 255:
            raise ValueError("invalid idempotency key")
        request_hash = canonical_intake_hash(command.source)
        profile = self.profile_resolver.resolve(command.tenant_slug)
        if command.source.document is not None and not any(
            item.name == "rate_confirmation" for item in profile.config.intake_types
        ):
            raise ValueError("tenant profile does not support rate confirmations")

        async with self.session.begin():
            existing = await self.repository.get_idempotency_for_tenant(
                command.tenant_id, key, for_update=True
            )
            if existing is not None:
                return self._existing_result(existing, request_hash)

            try:
                async with self.session.begin_nested():
                    job = await self.repository.create_job(
                        EnqueueIntakeCommand(
                            tenant_id=command.tenant_id,
                            tenant_slug=command.tenant_slug,
                            source=command.source,
                            idempotency_key=key,
                            risk_signals=command.risk_signals,
                        ),
                        profile,
                    )
                    await self.session.flush()
                    await self.repository.create_idempotency_record(
                        command.tenant_id, key, request_hash, job.id
                    )
            except IntegrityError:
                existing = await self.repository.get_idempotency_for_tenant(
                    command.tenant_id, key, for_update=True
                )
                if existing is None:
                    raise
                return self._existing_result(existing, request_hash)
            return EnqueueResult(job_id=job.id, created=True)

    @staticmethod
    def _existing_result(
        existing: IdempotencyRecord, request_hash: str
    ) -> EnqueueResult:
        if existing.request_hash != request_hash:
            raise IdempotencyConflict("idempotency key conflicts with request")
        if existing.job_id is None:
            raise RuntimeError("idempotency record has no job")
        return EnqueueResult(job_id=existing.job_id, created=False)
