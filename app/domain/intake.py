"""Closed inbound intake contract and idempotent job enqueue service."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, field
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import IdempotencyRecord
from app.documents import DocumentInput, DocumentNormalizer
from app.documents.registry import DocumentCapabilityUnavailable
from app.observability import (
    Component,
    EventLevel,
    EventName,
    NULL_OBSERVER,
    ObservationContext,
    ObservationEvent,
    Observer,
    OutcomeCode,
    PersistenceOperation,
    safe_emit,
)
from app.policy.models import RiskSignals, TrustedSource
from app.domain.job_repository import JobRepository
from app.domain.schemas import MAX_BODY_CHARS, MAX_CHANNEL_CHARS, MAX_SUBJECT_CHARS
from app.runtime.profiles import (
    ResolvedTenantProfile,
    TenantProfileResolver,
    resolve_persisted_profile,
)


class CreateIntakeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    channel: str = Field(min_length=1, max_length=MAX_CHANNEL_CHARS)
    subject: str = Field(min_length=1, max_length=MAX_SUBJECT_CHARS)
    body: str = Field(min_length=1, max_length=MAX_BODY_CHARS)

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
    trace_id: UUID = field(default_factory=uuid4)
    risk_signals: RiskSignals = field(default_factory=RiskSignals)


@dataclass(frozen=True, slots=True)
class EnqueueDocumentIntakeCommand:
    tenant_id: UUID
    tenant_slug: str
    document: DocumentInput
    idempotency_key: str
    trace_id: UUID = field(default_factory=uuid4)
    risk_signals: RiskSignals = field(default_factory=RiskSignals)
    sender: str | None = None


@dataclass(frozen=True, slots=True)
class EnqueueResult:
    job_id: UUID
    created: bool
    trace_id: UUID


class IdempotencyConflict(ValueError):
    """An idempotency key was reused with a different request body."""


def canonical_intake_hash(source: TrustedSource) -> str:
    payload = {
        "body": source.body,
        "channel": source.channel,
        "subject": source.subject,
    }
    if source.sender is not None:
        payload["sender"] = source.sender
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


def canonical_document_intake_hash(command: EnqueueDocumentIntakeCommand) -> str:
    """Hash the original document envelope independently of profile bindings."""

    document = command.document
    raw = (
        document.text.encode("utf-8")
        if document.media_type.value == "text/plain"
        else document.content or b""
    )
    payload: dict[str, object] = {
        "body": document.body,
        "channel": document.channel,
        "subject": document.subject,
        "document": {
            "media_type": document.media_type.value,
            "payload_sha256": hashlib.sha256(raw).hexdigest(),
        },
    }
    if command.sender is not None:
        payload["sender"] = command.sender
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
        *,
        observer: Observer = NULL_OBSERVER,
        context: ObservationContext | None = None,
    ) -> None:
        self.session = session
        self.repository = JobRepository(session)
        self.profile_resolver = profile_resolver
        self.observer = observer
        self.context = context
        self.document_normalizer = document_normalizer

    async def enqueue(self, command: EnqueueIntakeCommand) -> EnqueueResult:
        profile = self.profile_resolver.resolve(command.tenant_slug)
        self._emit_profile_resolution(command, profile)
        return await self._enqueue(command, profile=profile)

    async def enqueue_document(
        self, command: EnqueueDocumentIntakeCommand
    ) -> EnqueueResult:
        key = self._validate_idempotency_key(command.idempotency_key)
        request_hash = canonical_document_intake_hash(command)
        try:
            existing_result = await self._existing_document_result(
                command,
                key=key,
                request_hash=request_hash,
            )
        except IdempotencyConflict:
            profile = self.profile_resolver.resolve(command.tenant_slug)
            self._emit_profile_resolution(command, profile)
            self._emit_enqueue(
                EnqueueResult(
                    job_id=UUID(int=0),
                    created=False,
                    trace_id=command.trace_id,
                ),
                command=self._document_probe_command(command),
                profile=profile,
                outcome=OutcomeCode.CONFLICT,
                include_job=False,
            )
            raise
        if existing_result is not None:
            return existing_result

        profile = self.profile_resolver.resolve(command.tenant_slug)
        self._emit_profile_resolution(command, profile)
        compiled = profile.compiled
        if compiled is None or compiled.default_inbound_document is None:
            raise ValueError("tenant profile has no inbound document binding")
        binding = compiled.default_inbound_document
        if command.document.media_type not in binding.capability.supported_media_types:
            raise DocumentCapabilityUnavailable("document capability unavailable")
        context = self._context_for(command, profile=profile)
        normalizer = self.document_normalizer or binding.capability.create(
            observer=self.observer,
            context=context,
        )
        if isinstance(normalizer, DocumentNormalizer):
            normalizer.context = context
        normalized = await asyncio.to_thread(
            normalizer.normalize,
            command.document,
            document_kind=binding.document_kind,
            target_intake_type=binding.target_intake_type,
            normalizer_key=binding.normalizer_key,
            normalizer_version=binding.normalizer_version,
        )
        source = TrustedSource(
            channel=command.document.channel,
            sender=command.sender,
            subject=command.document.subject,
            body="Document received.",
            document=normalized,
        )
        return await self._enqueue(
            EnqueueIntakeCommand(
                tenant_id=command.tenant_id,
                tenant_slug=command.tenant_slug,
                source=source,
                idempotency_key=command.idempotency_key,
                trace_id=command.trace_id,
                risk_signals=command.risk_signals,
            ),
            profile=profile,
            request_hash=request_hash,
        )

    async def _enqueue(
        self,
        command: EnqueueIntakeCommand,
        *,
        profile: ResolvedTenantProfile,
        request_hash: str | None = None,
    ) -> EnqueueResult:
        key = self._validate_idempotency_key(command.idempotency_key)
        request_hash = request_hash or canonical_intake_hash(command.source)
        try:
            async with self.session.begin():
                existing = await self.repository.get_idempotency_for_tenant(
                    command.tenant_id, key, for_update=True
                )
                if existing is not None:
                    return await self._existing_result(
                        existing, request_hash, command=command
                    )

                try:
                    async with self.session.begin_nested():
                        job = await self.repository.create_job(
                            EnqueueIntakeCommand(
                                tenant_id=command.tenant_id,
                                tenant_slug=command.tenant_slug,
                                source=command.source,
                                idempotency_key=key,
                                trace_id=command.trace_id,
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
                    return await self._existing_result(
                        existing, request_hash, command=command
                    )
                result = EnqueueResult(
                    job_id=job.id,
                    created=True,
                    trace_id=command.trace_id,
                )
                self._emit_enqueue(
                    result,
                    command=command,
                    profile=profile,
                    outcome=OutcomeCode.CREATED,
                )
                return result
        except IdempotencyConflict:
            self._emit_enqueue(
                EnqueueResult(
                    job_id=UUID(int=0),
                    created=False,
                    trace_id=command.trace_id,
                ),
                command=command,
                profile=profile,
                outcome=OutcomeCode.CONFLICT,
                include_job=False,
            )
            raise
        except Exception:
            self._emit_persistence_failure(command, profile=profile)
            raise

    @staticmethod
    def _validate_idempotency_key(value: str) -> str:
        key = value.strip()
        if not key or len(key) > 255:
            raise ValueError("invalid idempotency key")
        return key

    @staticmethod
    def _document_probe_command(
        command: EnqueueDocumentIntakeCommand,
    ) -> EnqueueIntakeCommand:
        return EnqueueIntakeCommand(
            tenant_id=command.tenant_id,
            tenant_slug=command.tenant_slug,
            source=TrustedSource(
                channel=command.document.channel,
                sender=command.sender,
                subject=command.document.subject,
                body="Document received.",
            ),
            idempotency_key=command.idempotency_key,
            trace_id=command.trace_id,
            risk_signals=command.risk_signals,
        )

    async def _existing_document_result(
        self,
        command: EnqueueDocumentIntakeCommand,
        *,
        key: str,
        request_hash: str,
    ) -> EnqueueResult | None:
        probe = self._document_probe_command(command)
        async with self.session.begin():
            existing = await self.repository.get_idempotency_for_tenant(
                command.tenant_id,
                key,
                for_update=True,
            )
            if existing is None:
                return None
            return await self._existing_result(
                existing,
                request_hash,
                command=probe,
            )

    async def _existing_result(
        self,
        existing: IdempotencyRecord,
        request_hash: str,
        *,
        command: EnqueueIntakeCommand,
    ) -> EnqueueResult:
        if existing.request_hash != request_hash:
            raise IdempotencyConflict("idempotency key conflicts with request")
        if existing.job_id is None:
            raise RuntimeError("idempotency record has no job")
        identity = await self.repository.get_observation_identity(
            existing.job_id,
            tenant_id=command.tenant_id,
        )
        if identity is None:
            raise RuntimeError("idempotency record has no observation identity")
        profile = resolve_persisted_profile(
            identity.tenant_config_snapshot,
            identity.tenant_config_sha256,
            tenant_slug=command.tenant_slug,
        )
        self._emit_profile_resolution(command, profile, trace_id=identity.trace_id)
        result = EnqueueResult(
            job_id=existing.job_id,
            created=False,
            trace_id=identity.trace_id,
        )
        self._emit_enqueue(
            result,
            command=command,
            profile=profile,
            outcome=OutcomeCode.REUSED,
        )
        return result

    def _context_for(
        self,
        command: EnqueueIntakeCommand,
        *,
        trace_id: UUID | None = None,
        job_id: UUID | None = None,
        profile: ResolvedTenantProfile | None = None,
    ) -> ObservationContext:
        context = self.context or ObservationContext(trace_id=command.trace_id)
        bound = context.bind(
            trace_id=trace_id or command.trace_id,
            tenant_id=command.tenant_id,
            job_id=job_id,
        )
        if profile is not None and profile.compiled is not None:
            bound = bound.bind(
                scenario_key=profile.compiled.scenario_key,
                profile_fingerprint=profile.compiled.profile_fingerprint,
            )
        return bound

    def _emit_profile_resolution(
        self,
        command: EnqueueIntakeCommand | EnqueueDocumentIntakeCommand,
        profile: ResolvedTenantProfile,
        *,
        trace_id: UUID | None = None,
    ) -> None:
        context = self._context_for(
            command,  # type: ignore[arg-type]
            trace_id=trace_id,
            profile=profile,
        )
        safe_emit(
            self.observer,
            ObservationEvent(
                event=EventName.PROFILE_RESOLUTION_COMPLETED,
                trace_id=context.trace_id,
                request_id=context.request_id,
                tenant_id=context.tenant_id,
                job_id=context.job_id,
                component=Component.PROFILE,
                outcome=OutcomeCode.SUCCESS,
                scenario_key=context.scenario_key,
                profile_fingerprint=context.profile_fingerprint,
            ),
        )

    def _emit_enqueue(
        self,
        result: EnqueueResult,
        *,
        command: EnqueueIntakeCommand,
        profile: ResolvedTenantProfile,
        outcome: OutcomeCode,
        include_job: bool = True,
    ) -> None:
        context = self._context_for(
            command,
            trace_id=result.trace_id,
            job_id=result.job_id if include_job else None,
            profile=profile,
        )
        safe_emit(
            self.observer,
            ObservationEvent(
                event=EventName.INTAKE_ENQUEUE_COMPLETED,
                trace_id=context.trace_id,
                request_id=context.request_id,
                tenant_id=context.tenant_id,
                job_id=context.job_id,
                component=Component.INTAKE,
                outcome=outcome,
                scenario_key=context.scenario_key,
                profile_fingerprint=context.profile_fingerprint,
            ),
        )

    def _emit_persistence_failure(
        self,
        command: EnqueueIntakeCommand,
        *,
        profile: ResolvedTenantProfile | None = None,
    ) -> None:
        context = self._context_for(command, profile=profile)
        safe_emit(
            self.observer,
            ObservationEvent(
                event=EventName.PERSISTENCE_OPERATION_FAILED,
                level=EventLevel.ERROR,
                trace_id=context.trace_id,
                request_id=context.request_id,
                tenant_id=context.tenant_id,
                component=Component.PERSISTENCE,
                outcome=OutcomeCode.FAILED,
                persistence_operation=PersistenceOperation.ENQUEUE_JOB,
                scenario_key=context.scenario_key,
                profile_fingerprint=context.profile_fingerprint,
            ),
        )
