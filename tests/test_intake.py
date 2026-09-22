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
    EnqueueIntakeCommand,
    IdempotencyConflict,
    IntakeEnqueueService,
    canonical_intake_hash,
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
