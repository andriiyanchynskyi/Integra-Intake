"""Unit and HTTP contracts for the Phase-7 intake acceptance boundary."""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncGenerator
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, replace
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
    RateConfirmationDocumentInput,
)
from app.main import app
from app.policy import TrustedSource
from app.runtime.profiles import ResolvedTenantProfile
from app.tenants.loader import load_tenant_config


PROJECT_ROOT = Path(__file__).resolve().parents[1]


@dataclass
class _ScalarResult:
    value: IdempotencyRecord | None

    def scalar_one_or_none(self) -> IdempotencyRecord | None:
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

    async def execute(self, statement: object) -> _ScalarResult:
        compiled = statement.compile(dialect=postgresql.dialect())
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
    config = load_tenant_config(PROJECT_ROOT / "examples" / "freight-broker.yaml")
    snapshot = config.model_dump(mode="json")
    encoded = json.dumps(
        snapshot,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return ResolvedTenantProfile(
        config=config,
        snapshot=snapshot,
        sha256=hashlib.sha256(encoded).hexdigest(),
    )


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
) -> RateConfirmationDocumentInput:
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
    return RateConfirmationDocumentInput.model_validate(payload)


def _document_command(
    tenant_id: UUID,
    *,
    document: RateConfirmationDocumentInput | None = None,
    key: str = "document-1",
) -> EnqueueDocumentIntakeCommand:
    return EnqueueDocumentIntakeCommand(
        tenant_id=tenant_id,
        tenant_slug="freight-broker",
        document=document or _document_input(),
        idempotency_key=key,
    )


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
    first_document = DocumentNormalizer().normalize(_document_input(text="Origin: Chicago"))
    changed_document = DocumentNormalizer().normalize(_document_input(text="Origin: Detroit"))
    malformed_document = DocumentNormalizer().normalize(
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
    document = DocumentNormalizer().normalize(_document_input())
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
async def test_inbound_provider_id_reuses_per_tenant_and_conflicts_on_changed_source() -> None:
    session = _IntakeSession()
    service = IntakeEnqueueService(session, _ProfileResolver(_profile()))
    tenant_id = uuid4()
    provider_key = "email_webhook:provider-duplicate-1"
    normalized = DocumentNormalizer().normalize(_document_input(text="Origin: Chicago"))
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
            document=DocumentNormalizer().normalize(
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
async def test_enqueue_document_stores_normalized_document_snapshot_and_safe_body() -> None:
    session = _IntakeSession()
    service = IntakeEnqueueService(session, _ProfileResolver(_profile()))
    document = _document_input(text="Origin: Chicago\r\nDestination: Detroit")
    command = _document_command(uuid4(), document=document)

    result = await service.enqueue_document(command)

    assert result.created is True
    assert len(session.jobs) == 1
    snapshot = session.jobs[0].source_snapshot
    assert snapshot["channel"] == "email"
    assert snapshot["subject"] == "Rate confirmation"
    assert snapshot["body"] == "Rate confirmation document received."
    assert snapshot["document"] == DocumentNormalizer().normalize(document).model_dump(
        mode="json"
    )
    assert set(snapshot) == {"channel", "subject", "body", "document"}
    assert snapshot["document"]["text"] == (  # type: ignore[index]
        "Origin: Chicago\nDestination: Detroit"
    )


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
