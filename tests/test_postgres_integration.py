"""Live PostgreSQL coverage for Phase 2 authentication, cases, and seeding."""

from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import event, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth import generate_api_key, get_current_tenant
from app.db.models import ApiKey, CaseEvent, IntakeCase, Tenant
from app.domain.schemas import CreateCaseRequest
from app.domain.service import CaseService
from scripts.seed_demo import seed_demo_tenant


async def create_tenant_with_key(
    session_factory: async_sessionmaker[AsyncSession], name: str
) -> tuple[Tenant, str]:
    raw_key, prefix, key_hash = generate_api_key()
    tenant = Tenant(
        slug=f"{name.lower()}-{uuid4().hex}",
        name=name,
        status="active",
    )
    async with session_factory() as session:
        session.add(tenant)
        await session.flush()
        session.add(
            ApiKey(
                tenant_id=tenant.id,
                prefix=prefix,
                key_hash=key_hash,
                is_active=True,
            )
        )
        await session.commit()
    return tenant, raw_key


async def test_postgres_auth_resolves_only_an_active_persisted_key(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Authentication must execute its real UUID/digest query against PostgreSQL."""
    tenant, raw_key = await create_tenant_with_key(postgres_session_factory, "Auth Tenant")

    async with postgres_session_factory() as session:
        resolved = await get_current_tenant(x_api_key=raw_key, session=session)
        assert resolved.id == tenant.id

        with pytest.raises(HTTPException) as error:
            await get_current_tenant(x_api_key="ik_" + "A" * 43, session=session)

    assert error.value.status_code == 401


async def test_postgres_case_service_commits_case_and_event_together(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The real transaction must persist both the received case and its created audit event."""
    tenant, _ = await create_tenant_with_key(postgres_session_factory, "Service Tenant")
    request = CreateCaseRequest(channel="email", subject="Load", body="Need a truck")

    async with postgres_session_factory() as session:
        case = await CaseService(session).create_case(tenant.id, request)
        event_count = await session.scalar(
            select(func.count()).select_from(CaseEvent).where(CaseEvent.case_id == case.id)
        )

    assert case.channel == "email"
    assert case.subject == "Load"
    assert case.body == "Need a truck"
    assert event_count == 1


async def test_postgres_case_service_rolls_back_when_audit_event_cannot_persist(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A database rejection during audit flush must leave no received case behind."""
    tenant, _ = await create_tenant_with_key(postgres_session_factory, "Rollback Tenant")
    request = CreateCaseRequest(channel="email", subject="Load", body="Need a truck")

    async with postgres_session_factory() as session:
        def invalidate_created_event(sync_session, flush_context, instances) -> None:
            for instance in sync_session.new:
                if isinstance(instance, CaseEvent):
                    instance.event_type = None

        event.listen(session.sync_session, "before_flush", invalidate_created_event)
        try:
            with pytest.raises(IntegrityError):
                await CaseService(session).create_case(tenant.id, request)
        finally:
            event.remove(session.sync_session, "before_flush", invalidate_created_event)

    async with postgres_session_factory() as session:
        count = await session.scalar(
            select(func.count()).select_from(IntakeCase).where(IntakeCase.tenant_id == tenant.id)
        )

    assert count == 0


async def test_postgres_http_case_create_and_read_are_tenant_isolated(
    postgres_client, postgres_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """HTTP persistence must create for the authenticated tenant and hide it from another one."""
    owner, owner_key = await create_tenant_with_key(postgres_session_factory, "Owner Tenant")
    _, other_key = await create_tenant_with_key(postgres_session_factory, "Other Tenant")

    created = await postgres_client.post(
        "/v1/cases",
        headers={"X-API-Key": owner_key},
        json={"channel": "email", "subject": "Load", "body": "Need a truck"},
    )

    assert created.status_code == 201
    payload = created.json()
    assert payload["channel"] == "email"
    assert payload["subject"] == "Load"
    assert payload["body"] == "Need a truck"
    assert payload["created_at"]
    assert "tenant_id" not in payload

    owned = await postgres_client.get(
        f"/v1/cases/{payload['id']}", headers={"X-API-Key": owner_key}
    )
    hidden = await postgres_client.get(
        f"/v1/cases/{payload['id']}", headers={"X-API-Key": other_key}
    )

    assert owned.status_code == 200
    assert owned.json()["id"] == payload["id"]
    assert hidden.status_code == 404
    async with postgres_session_factory() as session:
        persisted_case = await session.get(IntakeCase, UUID(payload["id"]))
    assert persisted_case is not None
    assert persisted_case.tenant_id == owner.id


async def test_postgres_seed_rotates_a_digest_only_key_against_migrated_schema(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The seed routine must use the migrated tenant/API-key schema, not a unit double."""
    slug = f"seed-{uuid4().hex}"
    async with postgres_session_factory() as session:
        first_raw_key = await seed_demo_tenant(session, slug)
    async with postgres_session_factory() as session:
        second_raw_key = await seed_demo_tenant(session, slug)
    async with postgres_session_factory() as session:
        tenant = await session.scalar(select(Tenant).where(Tenant.slug == slug))
        keys = (
            await session.scalars(select(ApiKey).where(ApiKey.tenant_id == tenant.id))
        ).all()

    assert tenant is not None
    assert len(keys) == 2
    assert sum(key.is_active for key in keys) == 1
    active_key = next(key for key in keys if key.is_active)
    assert active_key.prefix == second_raw_key[:11]
    assert active_key.key_hash != second_raw_key
    assert first_raw_key != second_raw_key
