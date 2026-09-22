"""Live PostgreSQL coverage for the tenant-scoped Phase-7 tool port."""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session

from app.db.models import (
    AgentJob,
    CaseEvent,
    Customer,
    IdempotencyRecord,
    IntakeCase,
    Tenant,
)
from app.policy import TrustedSource
from app.tools.ports import CustomerNotFoundError
from app.tools.postgres import PostgresTenantToolPort


async def _create_tenant(
    session_factory: async_sessionmaker[AsyncSession],
    name: str,
) -> Tenant:
    tenant = Tenant(
        id=uuid4(),
        slug=f"{name.lower().replace(' ', '-')}-{uuid4().hex}",
        name=name,
        status="active",
    )
    async with session_factory() as session:
        session.add(tenant)
        await session.commit()
    return tenant


async def _create_customer(
    session_factory: async_sessionmaker[AsyncSession],
    tenant_id: UUID,
    *,
    external_id: str,
    email: str,
) -> Customer:
    customer = Customer(
        id=uuid4(),
        tenant_id=tenant_id,
        external_id=external_id,
        email=email,
        name=f"Customer {external_id}",
        phone=None,
        attributes={},
    )
    async with session_factory() as session:
        session.add(customer)
        await session.commit()
    return customer


async def _create_job_with_idempotency(
    session_factory: async_sessionmaker[AsyncSession],
    tenant_id: UUID,
    *,
    case_id: UUID | None = None,
) -> AgentJob:
    job = AgentJob(
        id=uuid4(),
        tenant_id=tenant_id,
        status="queued",
        source_snapshot={
            "channel": "email",
            "subject": "Load request",
            "body": "Need a truck",
        },
        tenant_config_snapshot={"slug": "freight-broker"},
        tenant_config_sha256="a" * 64,
        risk_signals={"safety_or_legal_risk": False},
    )
    record = IdempotencyRecord(
        id=uuid4(),
        tenant_id=tenant_id,
        key=f"job-{job.id}",
        request_hash="b" * 64,
        job_id=job.id,
        case_id=case_id,
        response={},
    )
    async with session_factory() as session:
        session.add_all([job, record])
        await session.commit()
    return job


async def _create_case(
    session_factory: async_sessionmaker[AsyncSession],
    tenant_id: UUID,
    *,
    fields: dict[str, object],
) -> IntakeCase:
    case = IntakeCase(
        id=uuid4(),
        tenant_id=tenant_id,
        customer_id=None,
        status="received",
        channel="email",
        subject="Existing case",
        body="Existing body",
        source="email",
        raw_payload={},
        extracted_fields=fields,
    )
    async with session_factory() as session:
        session.add(case)
        await session.commit()
    return case


async def _get_job_and_record(
    session_factory: async_sessionmaker[AsyncSession],
    job_id: UUID,
) -> tuple[AgentJob, IdempotencyRecord]:
    async with session_factory() as session:
        job = await session.get(AgentJob, job_id)
        record = await session.scalar(
            select(IdempotencyRecord).where(IdempotencyRecord.job_id == job_id)
        )
    assert job is not None
    assert record is not None
    return job, record


@pytest.mark.asyncio
async def test_postgres_port_hides_other_tenant_customers_and_cases(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_a = await _create_tenant(postgres_session_factory, "Tenant A")
    tenant_b = await _create_tenant(postgres_session_factory, "Tenant B")
    customer_a = await _create_customer(
        postgres_session_factory,
        tenant_a.id,
        external_id="a-1",
        email="a@example.com",
    )
    customer_b = await _create_customer(
        postgres_session_factory,
        tenant_b.id,
        external_id="b-1",
        email="b@example.com",
    )
    case_b = await _create_case(
        postgres_session_factory,
        tenant_b.id,
        fields={"summary": "Tenant B"},
    )
    job_a = await _create_job_with_idempotency(postgres_session_factory, tenant_a.id)
    port = PostgresTenantToolPort(postgres_session_factory, job_id=job_a.id)

    found = await port.find_customer(
        tenant_a.id,
        email=customer_a.email,
        external_id=None,
    )
    assert found is not None
    assert found.id == str(customer_a.id)
    assert (
        await port.find_customer(
            tenant_a.id,
            email=customer_b.email,
            external_id=None,
        )
    ) is None
    assert await port.case_exists(tenant_b.id, case_b.id) is True
    assert await port.case_exists(tenant_a.id, case_b.id) is False


@pytest.mark.asyncio
async def test_create_case_rejects_cross_tenant_customer_without_mutation(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_a = await _create_tenant(postgres_session_factory, "Tenant A")
    tenant_b = await _create_tenant(postgres_session_factory, "Tenant B")
    customer_b = await _create_customer(
        postgres_session_factory,
        tenant_b.id,
        external_id="b-1",
        email="b@example.com",
    )
    job_a = await _create_job_with_idempotency(postgres_session_factory, tenant_a.id)
    port = PostgresTenantToolPort(postgres_session_factory, job_id=job_a.id)

    with pytest.raises(CustomerNotFoundError):
        await port.create_case(
            tenant_a.id,
            TrustedSource(channel="email", subject="Load", body="Need a truck"),
            customer_id=customer_b.id,
            fields={"summary": "Should not persist"},
        )

    async with postgres_session_factory() as session:
        case_count = await session.scalar(
            select(func.count())
            .select_from(IntakeCase)
            .where(IntakeCase.tenant_id == tenant_a.id)
        )
        event_count = await session.scalar(
            select(func.count())
            .select_from(CaseEvent)
            .where(CaseEvent.tenant_id == tenant_a.id)
        )
    job, record = await _get_job_and_record(postgres_session_factory, job_a.id)

    assert case_count == 0
    assert event_count == 0
    assert record.case_id is None
    assert job.side_effect_committed_at is None


@pytest.mark.asyncio
async def test_create_case_is_atomic_and_idempotent_for_one_job(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await _create_tenant(postgres_session_factory, "Tenant A")
    job = await _create_job_with_idempotency(postgres_session_factory, tenant.id)
    port = PostgresTenantToolPort(postgres_session_factory, job_id=job.id)
    source = TrustedSource(
        channel="email",
        subject="Load request",
        body="Need a truck",
    )

    first = await port.create_case(
        tenant.id,
        source,
        customer_id=None,
        fields={"summary": "First proposal", "priority": "normal"},
    )
    job_after_first, record_after_first = await _get_job_and_record(
        postgres_session_factory, job.id
    )
    assert first.status == "received"
    case_id = UUID(first.id)
    assert record_after_first.case_id == case_id
    assert job_after_first.side_effect_committed_at is not None

    second = await port.create_case(
        tenant.id,
        TrustedSource(channel="web", subject="Different", body="Ignored"),
        customer_id=None,
        fields={"summary": "Second proposal"},
    )
    job_after_second, record_after_second = await _get_job_and_record(
        postgres_session_factory, job.id
    )

    async with postgres_session_factory() as session:
        case = await session.get(IntakeCase, case_id)
        events = (
            await session.scalars(
                select(CaseEvent)
                .where(
                    CaseEvent.tenant_id == tenant.id,
                    CaseEvent.case_id == case_id,
                )
                .order_by(CaseEvent.created_at, CaseEvent.id)
            )
        ).all()

    assert second.id == first.id
    assert case is not None
    assert case.status == "received"
    assert case.channel == source.channel
    assert case.subject == source.subject
    assert case.body == source.body
    assert case.extracted_fields == {
        "summary": "First proposal",
        "priority": "normal",
    }
    assert len(events) == 1
    assert events[0].event_type == "created"
    assert record_after_second.case_id == case_id
    assert job_after_second.side_effect_committed_at == job_after_first.side_effect_committed_at


@pytest.mark.asyncio
async def test_update_case_fields_merges_fields_event_and_side_effect_marker_atomically(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await _create_tenant(postgres_session_factory, "Tenant A")
    case = await _create_case(
        postgres_session_factory,
        tenant.id,
        fields={"keep": "yes", "summary": "old"},
    )
    job = await _create_job_with_idempotency(
        postgres_session_factory,
        tenant.id,
        case_id=case.id,
    )
    port = PostgresTenantToolPort(postgres_session_factory, job_id=job.id)

    result = await port.update_case_fields(
        tenant.id,
        case.id,
        fields={"summary": "new", "another": "value"},
    )

    assert result is not None
    assert result.id == str(case.id)
    assert result.updated_fields == ["another", "summary"]

    async with postgres_session_factory() as session:
        persisted = await session.get(IntakeCase, case.id)
        events = (
            await session.scalars(
                select(CaseEvent)
                .where(
                    CaseEvent.tenant_id == tenant.id,
                    CaseEvent.case_id == case.id,
                )
                .order_by(CaseEvent.created_at, CaseEvent.id)
            )
        ).all()
    updated_job, _ = await _get_job_and_record(postgres_session_factory, job.id)

    assert persisted is not None
    assert persisted.extracted_fields == {
        "keep": "yes",
        "summary": "new",
        "another": "value",
    }
    assert len(events) == 1
    assert events[0].event_type == "fields_updated"
    assert events[0].payload == {"updated_fields": ["another", "summary"]}
    assert updated_job.side_effect_committed_at is not None


@pytest.mark.asyncio
async def test_update_case_fields_hides_cross_tenant_case_without_mutation(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_a = await _create_tenant(postgres_session_factory, "Tenant A")
    tenant_b = await _create_tenant(postgres_session_factory, "Tenant B")
    case_b = await _create_case(
        postgres_session_factory,
        tenant_b.id,
        fields={"summary": "Tenant B"},
    )
    job_a = await _create_job_with_idempotency(postgres_session_factory, tenant_a.id)
    port = PostgresTenantToolPort(postgres_session_factory, job_id=job_a.id)

    result = await port.update_case_fields(
        tenant_a.id,
        case_b.id,
        fields={"summary": "Attacker update"},
    )

    async with postgres_session_factory() as session:
        persisted = await session.get(IntakeCase, case_b.id)
        event_count = await session.scalar(
            select(func.count())
            .select_from(CaseEvent)
            .where(CaseEvent.case_id == case_b.id)
        )
    job, _ = await _get_job_and_record(postgres_session_factory, job_a.id)

    assert result is None
    assert persisted is not None
    assert persisted.extracted_fields == {"summary": "Tenant B"}
    assert event_count == 0
    assert job.side_effect_committed_at is None


def _invalidate_new_case_events(sync_session, flush_context, instances) -> None:
    for instance in sync_session.new:
        if isinstance(instance, CaseEvent):
            instance.event_type = None


@pytest.mark.asyncio
async def test_create_case_event_failure_rolls_back_case_idempotency_and_marker(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await _create_tenant(postgres_session_factory, "Tenant A")
    job = await _create_job_with_idempotency(postgres_session_factory, tenant.id)
    port = PostgresTenantToolPort(postgres_session_factory, job_id=job.id)
    event.listen(Session, "before_flush", _invalidate_new_case_events)
    try:
        with pytest.raises(IntegrityError):
            await port.create_case(
                tenant.id,
                TrustedSource(channel="email", subject="Load", body="Need a truck"),
                customer_id=None,
                fields={"summary": "Will roll back"},
            )
    finally:
        event.remove(Session, "before_flush", _invalidate_new_case_events)

    async with postgres_session_factory() as session:
        case_count = await session.scalar(
            select(func.count())
            .select_from(IntakeCase)
            .where(IntakeCase.tenant_id == tenant.id)
        )
        event_count = await session.scalar(
            select(func.count())
            .select_from(CaseEvent)
            .where(CaseEvent.tenant_id == tenant.id)
        )
    job_after, record_after = await _get_job_and_record(
        postgres_session_factory, job.id
    )

    assert case_count == 0
    assert event_count == 0
    assert record_after.case_id is None
    assert job_after.side_effect_committed_at is None


@pytest.mark.asyncio
async def test_update_case_event_failure_rolls_back_fields_and_marker(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await _create_tenant(postgres_session_factory, "Tenant A")
    case = await _create_case(
        postgres_session_factory,
        tenant.id,
        fields={"summary": "Original"},
    )
    job = await _create_job_with_idempotency(
        postgres_session_factory,
        tenant.id,
        case_id=case.id,
    )
    port = PostgresTenantToolPort(postgres_session_factory, job_id=job.id)
    event.listen(Session, "before_flush", _invalidate_new_case_events)
    try:
        with pytest.raises(IntegrityError):
            await port.update_case_fields(
                tenant.id,
                case.id,
                fields={"summary": "Should roll back"},
            )
    finally:
        event.remove(Session, "before_flush", _invalidate_new_case_events)

    async with postgres_session_factory() as session:
        persisted = await session.get(IntakeCase, case.id)
        event_count = await session.scalar(
            select(func.count())
            .select_from(CaseEvent)
            .where(CaseEvent.case_id == case.id)
        )
    job_after, _ = await _get_job_and_record(postgres_session_factory, job.id)

    assert persisted is not None
    assert persisted.extracted_fields == {"summary": "Original"}
    assert event_count == 0
    assert job_after.side_effect_committed_at is None
