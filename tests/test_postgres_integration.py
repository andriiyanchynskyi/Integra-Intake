"""Live PostgreSQL coverage for Phase 2 authentication, cases, and seeding."""

from collections.abc import Sequence
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import event, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent import AgentMessage, AgentProposal, ProposalPriority
from app.auth import generate_api_key, get_current_tenant
from app.core.config import Settings
from app.db.models import AgentJob, ApiKey, Approval, CaseEvent, IdempotencyRecord, IntakeCase, Tenant
from app.domain.schemas import CreateCaseRequest
from app.domain.service import CaseService
from app.runtime.factory import AgentRuntimeFactory
from app.tools.postgres import PostgresTenantToolPort
from app.workers.agent_worker import AgentWorker
from scripts.seed_demo import seed_demo_tenant


async def create_tenant_with_key(
    session_factory: async_sessionmaker[AsyncSession],
    name: str,
    *,
    slug: str | None = None,
) -> tuple[Tenant, str]:
    raw_key, prefix, key_hash = generate_api_key()
    tenant = Tenant(
        slug=slug or f"{name.lower()}-{uuid4().hex}",
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


class _CreateCaseLLM:
    """Deterministic provider double for the worker-to-PostgreSQL vertical path."""

    def __init__(self) -> None:
        self.calls = 0
        self._proposals = [
            AgentProposal.model_validate(
                {
                    "intake_type": "load_request",
                    "fields": [
                        {"name": "origin", "value": "Kyiv"},
                        {"name": "destination", "value": "Lviv"},
                        {"name": "equipment", "value": "dry_van"},
                        {"name": "pickup_window", "value": "2026-10-01T09:00:00Z"},
                        {"name": "commodity", "value": "Machine parts"},
                        {"name": "contact", "value": "shipper@example.com"},
                    ],
                    "missing_required_fields": [],
                    "priority": ProposalPriority.NORMAL,
                    "contains_injection_or_override_attempt": False,
                    "rationale_short": "Create the intake case.",
                    "tool_calls": [{"name": "create_case", "arguments": []}],
                    "confidence": 0.95,
                }
            ),
            AgentProposal.model_validate(
                {
                    "intake_type": "load_request",
                    "fields": [],
                    "missing_required_fields": [],
                    "priority": ProposalPriority.NORMAL,
                    "contains_injection_or_override_attempt": False,
                    "rationale_short": "Case created.",
                    "tool_calls": [],
                    "confidence": 0.95,
                }
            ),
        ]

    def complete(self, messages: Sequence[AgentMessage]) -> AgentProposal:
        self.calls += 1
        return self._proposals.pop(0)


@pytest.mark.asyncio
async def test_postgres_phase7_intake_worker_is_idempotent_and_tenant_scoped(
    postgres_client,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The accepted job reaches one real case transaction outside FastAPI."""
    owner, owner_key = await create_tenant_with_key(
        postgres_session_factory,
        "Freight Broker",
        slug="freight-broker",
    )
    other, other_key = await create_tenant_with_key(
        postgres_session_factory,
        "Other Tenant",
    )
    source_body = "Please quote a dry van from Kyiv to Lviv."
    headers = {"X-API-Key": owner_key, "Idempotency-Key": "phase7-vertical-1"}
    payload = {
        "channel": "email",
        "subject": "Dry van quote",
        "body": source_body,
    }

    accepted = await postgres_client.post("/v1/intake", headers=headers, json=payload)
    assert accepted.status_code == 202
    job_id = UUID(accepted.json()["job_id"])
    assert accepted.json()["status"] == "queued"

    llm_instances: list[_CreateCaseLLM] = []

    def llm_factory(_client: object) -> _CreateCaseLLM:
        llm = _CreateCaseLLM()
        llm_instances.append(llm)
        return llm

    runtime_factory = AgentRuntimeFactory(
        postgres_session_factory,
        llm_factory=llm_factory,
    )
    worker = AgentWorker(
        postgres_session_factory,
        runtime_factory=runtime_factory,
        settings=Settings(
            worker_poll_interval_seconds=0.001,
            worker_lease_seconds=60,
            worker_concurrency=1,
            worker_max_retries=4,
        ),
    )

    assert await worker.serve_once() is True
    assert len(llm_instances) == 1
    assert llm_instances[0].calls == 2

    repeated = await postgres_client.post("/v1/intake", headers=headers, json=payload)
    assert repeated.status_code == 202
    assert UUID(repeated.json()["job_id"]) == job_id
    assert await worker.serve_once() is False

    async with postgres_session_factory() as session:
        job = await session.get(AgentJob, job_id)
        idempotency = await session.scalar(
            select(IdempotencyRecord).where(
                IdempotencyRecord.tenant_id == owner.id,
                IdempotencyRecord.key == "phase7-vertical-1",
            )
        )
        cases = (
            await session.scalars(
                select(IntakeCase).where(IntakeCase.tenant_id == owner.id)
            )
        ).all()
        events = (
            await session.scalars(
                select(CaseEvent).where(
                    CaseEvent.tenant_id == owner.id,
                    CaseEvent.event_type == "created",
                )
            )
        ).all()
        approval_count = await session.scalar(
            select(func.count()).select_from(Approval).where(Approval.tenant_id == owner.id)
        )

    assert job is not None
    assert job.status == "succeeded"
    assert job.result is not None
    assert source_body not in repr(job.result)
    assert idempotency is not None
    assert idempotency.job_id == job_id
    assert idempotency.case_id == cases[0].id
    assert len(cases) == 1
    assert len(events) == 1
    assert events[0].case_id == cases[0].id
    assert approval_count == 0

    case_id = cases[0].id
    real_port = PostgresTenantToolPort(postgres_session_factory, job_id=job_id)
    assert await real_port.case_exists(owner.id, case_id) is True
    assert await real_port.case_exists(other.id, case_id) is False
    assert (
        await real_port.update_case_fields(
            other.id,
            case_id,
            fields={"origin": "attacker-controlled"},
        )
        is None
    )

    hidden = await postgres_client.get(
        f"/v1/cases/{case_id}", headers={"X-API-Key": other_key}
    )
    assert hidden.status_code == 404
