"""Unit and HTTP contracts for the Phase-7 intake acceptance boundary."""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncGenerator
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field, replace
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql

from app.auth import get_current_tenant
from app.db.models import AgentJob, IdempotencyRecord, Tenant
from app.db.session import get_db_session
from app.domain.intake import (
    CreateIntakeRequest,
    EnqueueDocumentIntakeCommand,
    EnqueueIntakeCommand,
    IdempotencyConflict,
    IntakeEnqueueService,
    canonical_intake_hash,
)
from app.documents import (
    DocumentMediaType,
    DocumentNormalizer,
    DocumentInput,
    NormalizedDocument,
)
from app.main import app
from app.observability import (
    EventName,
    ObservationContext,
    OutcomeCode,
    RecordingObserver,
)
from app.policy import TrustedSource
from app.runtime.profiles import ResolvedTenantProfile, TenantProfileResolver


PROJECT_ROOT = Path(__file__).resolve().parents[1]


@dataclass
class _ScalarResult:
    value: IdempotencyRecord | None

    def scalar_one_or_none(self) -> IdempotencyRecord | None:
        return self.value


@dataclass
class _UUIDScalarResult:
    value: UUID | None

    def scalar_one_or_none(self) -> UUID | None:
        return self.value


@dataclass
class _RowResult:
    value: tuple[UUID, dict[str, object], str] | None

    def one_or_none(self) -> tuple[UUID, dict[str, object], str] | None:
        return self.value


class _Transaction(AbstractAsyncContextManager["_Transaction"]):
    def __init__(self, session: "_IntakeSession", *, nested: bool) -> None:
        self.session = session
        self.nested = nested
        self.pending_jobs_at_entry = len(session.pending_jobs)
        self.pending_records_at_entry = len(session.pending_records)

    async def __aenter__(self) -> "_Transaction":
        self.session.transaction_depth += 1
        return self

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        self.session.transaction_depth -= 1
        if exc_type is not None:
            del self.session.pending_jobs[self.pending_jobs_at_entry :]
            del self.session.pending_records[self.pending_records_at_entry :]
        elif not self.nested:
            self.session.jobs.extend(self.session.pending_jobs)
            self.session.idempotency_records.extend(self.session.pending_records)
            self.session.pending_jobs.clear()
            self.session.pending_records.clear()


class _IntakeSession:
    """Small async-session double that preserves the service transaction shape."""

    def __init__(self) -> None:
        self.jobs: list[AgentJob] = []
        self.idempotency_records: list[IdempotencyRecord] = []
        self.pending_jobs: list[AgentJob] = []
        self.pending_records: list[IdempotencyRecord] = []
        self.transaction_depth = 0

    def begin(self) -> _Transaction:
        return _Transaction(self, nested=False)

    def begin_nested(self) -> _Transaction:
        return _Transaction(self, nested=True)

    def add(self, instance: AgentJob | IdempotencyRecord) -> None:
        if isinstance(instance, AgentJob):
            self.pending_jobs.append(instance)
        else:
            self.pending_records.append(instance)

    async def flush(self) -> None:
        for job in self.pending_jobs:
            if job.id is None:
                job.id = uuid4()
            if job.status is None:
                job.status = "queued"
            if job.attempt_count is None:
                job.attempt_count = 0
        for record in self.pending_records:
            if record.id is None:
                record.id = uuid4()

    async def execute(
        self, statement: object
    ) -> _ScalarResult | _UUIDScalarResult | _RowResult:
        compiled = statement.compile(dialect=postgresql.dialect())
        if "tenant_config_snapshot" in str(compiled):
            job = next(
                (item for item in self.jobs if item.id in compiled.params.values()),
                None,
            )
            return _RowResult(
                None
                if job is None
                else (
                    job.trace_id,
                    job.tenant_config_snapshot,
                    job.tenant_config_sha256,
                )
            )
        if "agent_jobs.trace_id" in str(compiled):
            job_id = next(
                value for value in compiled.params.values() if isinstance(value, UUID)
            )
            job = next((item for item in self.jobs if item.id == job_id), None)
            return _UUIDScalarResult(job.trace_id if job is not None else None)
        tenant_id = compiled.params["tenant_id_1"]
        key = compiled.params["key_1"]
        value = next(
            (
                record
                for record in self.idempotency_records
                if record.tenant_id == tenant_id and record.key == key
            ),
            None,
        )
        return _ScalarResult(value)


class _ProfileResolver:
    def __init__(self, profile: ResolvedTenantProfile) -> None:
        self.profile = profile
        self.slugs: list[str] = []

    def resolve(self, tenant_slug: str) -> ResolvedTenantProfile:
        self.slugs.append(tenant_slug)
        return self.profile


def _profile() -> ResolvedTenantProfile:
    return TenantProfileResolver(PROJECT_ROOT / "examples").resolve("freight-broker")


def _command(
    tenant_id: UUID,
    *,
    body: str = "Need a truck",
    key: str = "request-1",
) -> EnqueueIntakeCommand:
    return EnqueueIntakeCommand(
        tenant_id=tenant_id,
        tenant_slug="freight-broker",
        source=TrustedSource(channel="email", subject="Load", body=body),
        idempotency_key=key,
    )


def _document_input(
    *,
    text: str | None = "Origin: Chicago\r\nDestination: Detroit",
    content: bytes | None = None,
    media_type: DocumentMediaType = DocumentMediaType.TEXT,
) -> DocumentInput:
    payload: dict[str, object] = {
        "channel": "email",
        "subject": "Rate confirmation",
        "body": "Please process this document.",
        "media_type": media_type,
    }
    if text is not None:
        payload["text"] = text
    if content is not None:
        payload["content"] = content
    return DocumentInput.model_validate(payload)


def _document_command(
    tenant_id: UUID,
    *,
    document: DocumentInput | None = None,
    key: str = "document-1",
) -> EnqueueDocumentIntakeCommand:
    return EnqueueDocumentIntakeCommand(
        tenant_id=tenant_id,
        tenant_slug="freight-broker",
        document=document or _document_input(),
        idempotency_key=key,
    )


def _normalize(document: DocumentInput) -> NormalizedDocument:
    return DocumentNormalizer().normalize(
        document,
        document_kind="rate_confirmation",
        target_intake_type="rate_confirmation",
        normalizer_key="bounded_text_pdf",
        normalizer_version=1,
    )


@dataclass
class _RecordingNormalizer:
    result: NormalizedDocument
    calls: list[tuple[DocumentInput, dict[str, object]]] = field(default_factory=list)

    def normalize(self, value: DocumentInput, **kwargs: object) -> NormalizedDocument:
        self.calls.append((value, kwargs))
        return self.result


def test_create_intake_request_forbids_extra_fields() -> None:
    with pytest.raises(ValidationError):
        CreateIntakeRequest(
            channel="email",
            subject="Load",
            body="Need a truck",
            tenant_id=uuid4(),
        )


@pytest.mark.parametrize("field", ["channel", "subject", "body"])
def test_create_intake_request_rejects_blank_required_values(field: str) -> None:
    payload = {"channel": "email", "subject": "Load", "body": "Need a truck"}
    payload[field] = " \t"

    with pytest.raises(ValidationError):
        CreateIntakeRequest(**payload)


def test_canonical_intake_hash_is_stable_and_content_sensitive() -> None:
    source = TrustedSource(channel="email", subject="Привіт", body="Need a truck")
    expected = json.dumps(
        {"body": source.body, "channel": source.channel, "subject": source.subject},
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")

    assert canonical_intake_hash(source) == hashlib.sha256(expected).hexdigest()
    assert canonical_intake_hash(replace(source, body="Different body")) != canonical_intake_hash(
        source
    )


def test_trusted_source_sender_is_optional_and_changes_only_new_hash() -> None:
    legacy = TrustedSource(channel="email", subject="Load", body="Need a truck")
    expected = json.dumps(
        {"body": legacy.body, "channel": legacy.channel, "subject": legacy.subject},
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")

    with_sender = replace(legacy, sender="dispatcher@example.test")

    assert legacy.sender is None
    assert canonical_intake_hash(legacy) == hashlib.sha256(expected).hexdigest()
    assert canonical_intake_hash(with_sender) != canonical_intake_hash(legacy)
    assert canonical_intake_hash(replace(legacy, sender="other@example.test")) != (
        canonical_intake_hash(with_sender)
    )


def test_canonical_intake_hash_includes_document_digest_and_outcome() -> None:
    source = TrustedSource(channel="email", subject="Rate", body="Document received")
    first_document = _normalize(_document_input(text="Origin: Chicago"))
    changed_document = _normalize(_document_input(text="Origin: Detroit"))
    malformed_document = _normalize(
        _document_input(
            media_type=DocumentMediaType.PDF,
            text=None,
            content=b"malformed-pdf-secret",
        )
    )

    first_hash = canonical_intake_hash(replace(source, document=first_document))
    changed_hash = canonical_intake_hash(replace(source, document=changed_document))
    malformed_hash = canonical_intake_hash(replace(source, document=malformed_document))

    assert first_document.sha256 != changed_document.sha256
    assert first_hash != changed_hash
    assert first_hash != malformed_hash


def test_trusted_source_keeps_document_optional_and_typed() -> None:
    document = _normalize(_document_input())
    legacy = TrustedSource(channel="email", subject="Load", body="Need a truck")
    with_document = TrustedSource(
        channel="email",
        subject="Rate confirmation",
        body="Rate confirmation document received.",
        document=document,
    )

    assert legacy.document is None
    assert with_document.document == document


@pytest.mark.asyncio
async def test_body_only_enqueue_keeps_the_legacy_exact_three_key_snapshot() -> None:
    session = _IntakeSession()
    service = IntakeEnqueueService(session, _ProfileResolver(_profile()))
    command = _command(uuid4())

    await service.enqueue(command)

    assert len(session.jobs) == 1
    assert session.jobs[0].source_snapshot == {
        "channel": "email",
        "subject": "Load",
        "body": "Need a truck",
    }


@pytest.mark.asyncio
async def test_sender_enqueue_snapshot_contains_the_fixed_inbound_source_shape() -> None:
    session = _IntakeSession()
    service = IntakeEnqueueService(session, _ProfileResolver(_profile()))
    command = EnqueueIntakeCommand(
        tenant_id=uuid4(),
        tenant_slug="freight-broker",
        source=TrustedSource(
            channel="email_webhook",
            sender="dispatcher@example.test",
            subject="Load",
            body="Need a truck",
        ),
        idempotency_key="email_webhook:provider-123",
    )

    result = await service.enqueue(command)

    assert result.created is True
    assert session.jobs[0].source_snapshot == {
        "channel": "email_webhook",
        "sender": "dispatcher@example.test",
        "subject": "Load",
        "body": "Need a truck",
    }


@pytest.mark.asyncio
async def test_enqueue_fresh_then_reuses_same_idempotency_record() -> None:
    session = _IntakeSession()
    resolver = _ProfileResolver(_profile())
    service = IntakeEnqueueService(session, resolver)
    command = _command(uuid4())

    first = await service.enqueue(command)
    second = await service.enqueue(command)

    assert first.created is True
    assert second.created is False
    assert second.job_id == first.job_id
    assert len(session.jobs) == 1
    assert len(session.idempotency_records) == 1
    assert resolver.slugs == ["freight-broker", "freight-broker"]


@pytest.mark.asyncio
async def test_duplicate_reuses_stored_trace_and_profile_identity_after_profile_change(
    tmp_path: Path,
) -> None:
    session = _IntakeSession()
    observer = RecordingObserver()
    tenant_id = uuid4()
    provisional = uuid4()
    duplicate_request_trace = uuid4()
    profile_dir = tmp_path / "profiles"
    profile_dir.mkdir()
    profile_path = profile_dir / "freight-broker.yaml"
    profile_path.write_text(
        (PROJECT_ROOT / "examples" / "freight-broker.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    resolver = TenantProfileResolver(profile_dir)
    original_profile = resolver.resolve("freight-broker")
    service = IntakeEnqueueService(
        session,
        resolver,
        observer=observer,
        context=ObservationContext(trace_id=provisional),
    )

    first = await service.enqueue(
        replace(_command(tenant_id), trace_id=provisional)
    )
    changed_yaml = profile_path.read_text(encoding="utf-8").replace(
        "display_name: Freight broker",
        "display_name: Freight broker changed",
        1,
    )
    profile_path.write_text(changed_yaml, encoding="utf-8")
    changed_profile = resolver.resolve("freight-broker")
    assert changed_profile.sha256 != original_profile.sha256

    second = await service.enqueue(
        replace(
            _command(tenant_id, key="request-1"),
            trace_id=duplicate_request_trace,
        )
    )

    assert first.trace_id == provisional
    assert second.trace_id == first.trace_id
    assert session.jobs[0].tenant_config_snapshot["scenario_key"] == "freight_broker"
    assert session.jobs[0].tenant_config_sha256 == original_profile.sha256
    events = [
        event
        for event in observer.events
        if event.event is EventName.INTAKE_ENQUEUE_COMPLETED
    ]
    assert [event.outcome for event in events] == [OutcomeCode.CREATED, OutcomeCode.REUSED]
    assert all(event.trace_id == first.trace_id for event in events)
    assert [event.scenario_key for event in events] == ["freight_broker"] * 2
    assert [event.profile_fingerprint for event in events] == [
        original_profile.sha256,
        original_profile.sha256,
    ]
    assert all("Need a truck" not in event.model_dump_json() for event in events)


@pytest.mark.asyncio
async def test_inbound_provider_id_reuses_per_tenant_and_conflicts_on_changed_source() -> None:
    session = _IntakeSession()
    service = IntakeEnqueueService(session, _ProfileResolver(_profile()))
    tenant_id = uuid4()
    provider_key = "email_webhook:provider-duplicate-1"
    normalized = _normalize(_document_input(text="Origin: Chicago"))
    source = TrustedSource(
        channel="email_webhook",
        sender="dispatcher@example.test",
        subject="Rate confirmation",
        body="Please process the attachment.",
        document=normalized,
    )
    command = EnqueueIntakeCommand(
        tenant_id=tenant_id,
        tenant_slug="freight-broker",
        source=source,
        idempotency_key=provider_key,
    )

    first = await service.enqueue(command)
    duplicate = await service.enqueue(command)

    assert first.created is True
    assert duplicate.created is False
    assert duplicate.job_id == first.job_id
    assert len(session.jobs) == 1
    assert len(session.idempotency_records) == 1
    assert session.idempotency_records[0].key == provider_key

    changed_sources = (
        replace(source, body="A different message."),
        replace(source, sender="other-dispatcher@example.test"),
        replace(
            source,
            document=_normalize(
                _document_input(text="Origin: Detroit")
            ),
        ),
    )
    for changed_source in changed_sources:
        with pytest.raises(IdempotencyConflict):
            await service.enqueue(replace(command, source=changed_source))

    other_tenant_id = uuid4()
    other_tenant = await service.enqueue(replace(command, tenant_id=other_tenant_id))
    assert other_tenant.created is True
    assert other_tenant.job_id != first.job_id
    assert len(session.jobs) == 2
    assert len(session.idempotency_records) == 2
    assert {record.tenant_id for record in session.idempotency_records} == {
        tenant_id,
        other_tenant_id,
    }


@pytest.mark.asyncio
async def test_enqueue_document_uses_compiled_default_binding_and_generic_body_marker() -> None:
    session = _IntakeSession()
    document = _document_input(text="Origin: Chicago\r\nDestination: Detroit")
    normalized = NormalizedDocument(
        document_kind="rate_confirmation",
        target_intake_type="rate_confirmation",
        media_type=DocumentMediaType.TEXT,
        sha256="a" * 64,
        normalizer_key="bounded_text_pdf",
        normalizer_version=1,
        text="Origin: Chicago\nDestination: Detroit",
    )
    normalizer = _RecordingNormalizer(normalized)
    service = IntakeEnqueueService(
        session,
        _ProfileResolver(_profile()),
        document_normalizer=normalizer,
    )
    command = _document_command(uuid4(), document=document)

    result = await service.enqueue_document(command)

    assert result.created is True
    assert normalizer.calls == [
        (
            document,
            {
                "document_kind": "rate_confirmation",
                "target_intake_type": "rate_confirmation",
                "normalizer_key": "bounded_text_pdf",
                "normalizer_version": 1,
            },
        )
    ]
    assert len(session.jobs) == 1
    snapshot = session.jobs[0].source_snapshot
    assert snapshot["channel"] == "email"
    assert snapshot["subject"] == "Rate confirmation"
    assert snapshot["body"] == "Document received."
    assert snapshot["document"] == normalized.model_dump(mode="json")
    assert set(snapshot) == {"channel", "subject", "body", "document"}
    assert snapshot["document"]["text"] == (  # type: ignore[index]
        "Origin: Chicago\nDestination: Detroit"
    )


@pytest.mark.asyncio
async def test_repair_document_intake_rejects_before_normalizer_call() -> None:
    session = _IntakeSession()
    normalizer = _RecordingNormalizer(
        NormalizedDocument(
            document_kind="rate_confirmation",
            target_intake_type="rate_confirmation",
            media_type=DocumentMediaType.TEXT,
            sha256="b" * 64,
            normalizer_key="bounded_text_pdf",
            normalizer_version=1,
            text="not reached",
        )
    )
    service = IntakeEnqueueService(
        session,
        _ProfileResolver(
            TenantProfileResolver(PROJECT_ROOT / "examples").resolve("repair-service")
        ),
        document_normalizer=normalizer,
    )

    with pytest.raises(ValueError):
        await service.enqueue_document(
            replace(_document_command(uuid4()), tenant_slug="repair-service")
        )

    assert normalizer.calls == []
    assert session.jobs == []


@pytest.mark.asyncio
async def test_enqueue_document_reuses_same_tenant_key_and_document() -> None:
    session = _IntakeSession()
    service = IntakeEnqueueService(session, _ProfileResolver(_profile()))
    command = _document_command(uuid4())

    first = await service.enqueue_document(command)
    second = await service.enqueue_document(command)

    assert first.created is True
    assert second.created is False
    assert second.job_id == first.job_id
    assert len(session.jobs) == 1
    assert len(session.idempotency_records) == 1


@pytest.mark.asyncio
async def test_enqueue_document_rejects_changed_document_for_reused_key() -> None:
    session = _IntakeSession()
    service = IntakeEnqueueService(session, _ProfileResolver(_profile()))
    tenant_id = uuid4()
    first = _document_command(
        tenant_id,
        document=_document_input(text="Origin: Chicago"),
        key="document-conflict",
    )
    changed = _document_command(
        tenant_id,
        document=_document_input(text="Origin: Detroit"),
        key="document-conflict",
    )

    await service.enqueue_document(first)

    with pytest.raises(IdempotencyConflict):
        await service.enqueue_document(changed)

    assert len(session.jobs) == 1
    assert len(session.idempotency_records) == 1


@pytest.mark.asyncio
async def test_document_snapshot_contains_only_safe_parser_error_metadata() -> None:
    raw_content = b"malformed-pdf-secret"
    session = _IntakeSession()
    service = IntakeEnqueueService(session, _ProfileResolver(_profile()))
    command = _document_command(
        uuid4(),
        document=_document_input(
            media_type=DocumentMediaType.PDF,
            text=None,
            content=raw_content,
        ),
    )

    result = await service.enqueue_document(command)

    snapshot = session.jobs[0].source_snapshot
    document_snapshot = snapshot["document"]
    assert result.created is True
    assert document_snapshot["text"] is None  # type: ignore[index]
    assert document_snapshot["extraction_error"] == "pdf_malformed"  # type: ignore[index]
    assert "content" not in document_snapshot  # type: ignore[operator]
    assert raw_content not in repr(snapshot).encode("utf-8")
    assert "PdfReadError" not in repr(snapshot)
    assert "malformed-pdf-secret" not in repr(snapshot)


@pytest.mark.asyncio
async def test_enqueue_rejects_idempotency_key_reuse_for_different_body() -> None:
    session = _IntakeSession()
    service = IntakeEnqueueService(session, _ProfileResolver(_profile()))
    command = _command(uuid4())
    await service.enqueue(command)

    with pytest.raises(IdempotencyConflict):
        await service.enqueue(replace(command, source=replace(command.source, body="Changed")))

    assert len(session.jobs) == 1
    assert len(session.idempotency_records) == 1


@pytest.fixture
def intake_client() -> TestClient:
    tenant = Tenant(id=uuid4(), slug="freight-broker", name="Freight broker")

    async def override_session() -> AsyncGenerator[object, None]:
        yield object()

    async def override_tenant() -> Tenant:
        return tenant

    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_current_tenant] = override_tenant
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.clear()


def test_post_intake_requires_idempotency_key(intake_client: TestClient) -> None:
    response = intake_client.post(
        "/v1/intake",
        headers={"X-API-Key": "ik_test"},
        json={"channel": "email", "subject": "Load", "body": "Need a truck"},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "Idempotency-Key is required"
