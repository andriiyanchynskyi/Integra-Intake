from __future__ import annotations

from collections.abc import Iterable
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth import AuthenticatedOperator
from app.db.models import (
    AgentJob,
    Approval,
    ApprovalEvent,
    CaseEvent,
    IdempotencyRecord,
    IntakeCase,
    Tenant,
)
from app.domain.approval_repository import ApprovalCreationConflict, ApprovalRepository
from app.domain.approvals import ApprovalDecisionRequest, ApprovalDecisionService
from app.tools.models import PendingAction
from app.tools.postgres import PostgresApprovalActionExecutor


TENANT_A = UUID("00000000-0000-0000-0000-000000000001")
TENANT_B = UUID("00000000-0000-0000-0000-000000000002")
DECISION_TIME = datetime(2026, 9, 23, 13, tzinfo=timezone.utc)


class _ScalarResult:
    def __init__(self, value: AgentJob | Approval | None) -> None:
        self.value = value

    def scalar_one_or_none(self) -> AgentJob | Approval | None:
        return self.value


class _Transaction(AbstractAsyncContextManager["_Transaction"]):
    async def __aenter__(self) -> "_Transaction":
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None


class _ApprovalSession:
    """Small async-session double that evaluates the repository's tenant filters."""

    def __init__(
        self,
        jobs: Iterable[AgentJob],
        approvals: Iterable[Approval] = (),
    ) -> None:
        self.jobs = list(jobs)
        self.approvals = list(approvals)
        self.events: list[ApprovalEvent] = []
        self.queries: list[str] = []

    def begin(self) -> _Transaction:
        return _Transaction()

    def add(self, value: Approval | ApprovalEvent) -> None:
        if isinstance(value, Approval):
            self.approvals.append(value)
        else:
            self.events.append(value)

    async def flush(self) -> None:
        for value in (*self.approvals, *self.events):
            if value.id is None:
                value.id = uuid4()

    async def execute(self, statement: object) -> _ScalarResult:
        compiled = statement.compile(dialect=postgresql.dialect())
        query = str(compiled)
        self.queries.append(query)
        params = compiled.params

        if "FROM agent_jobs" in query:
            job_id = params["id_1"]
            tenant_id = params["tenant_id_1"]
            return _ScalarResult(
                next(
                    (
                        job
                        for job in self.jobs
                        if job.id == job_id
                        and job.tenant_id == tenant_id
                    ),
                    None,
                )
            )

        assert "FROM approvals" in query
        tenant_id = params["tenant_id_1"]
        job_id = params["job_id_1"]
        return _ScalarResult(
            next(
                (
                    approval
                    for approval in self.approvals
                    if approval.tenant_id == tenant_id and approval.job_id == job_id
                ),
                None,
            )
        )


def _job(
    *,
    tenant_id: UUID = TENANT_A,
    job_id: UUID | None = None,
    status: str = "running",
    attempt_count: int = 2,
) -> AgentJob:
    now = datetime(2026, 9, 23, 12, tzinfo=timezone.utc)
    return AgentJob(
        id=job_id or uuid4(),
        tenant_id=tenant_id,
        status=status,
        source_snapshot={"channel": "email", "subject": "Load", "body": "Need help"},
        tenant_config_snapshot={"slug": "acme"},
        tenant_config_sha256="a" * 64,
        risk_signals={},
        attempt_count=attempt_count,
        available_at=now,
    )


def _action() -> PendingAction:
    return PendingAction(
        name="create_case",
        arguments={"customer_id": None},
        known_fields={"summary": "Create a case"},
    )


@pytest.mark.asyncio
async def test_create_for_running_job_locks_tenant_attempt_and_parks_job() -> None:
    now = datetime(2026, 9, 23, 13, tzinfo=timezone.utc)
    job = _job()
    session = _ApprovalSession([job])

    requested = await ApprovalRepository(session).create_for_running_job(
        TENANT_A,
        job.id,
        attempt_count=job.attempt_count,
        action=_action(),
        policy_reason="approval_required",
        expires_in=timedelta(hours=24),
        now=now,
    )

    assert job.status == "awaiting_approval"
    assert job.finished_at == now
    assert job.lease_expires_at is None
    assert job.error_code is None
    assert job.result == {
        "status": "awaiting_approval",
        "approval_id": str(requested.id),
        "policy_reason": "approval_required",
    }
    assert len(session.approvals) == 1
    approval = session.approvals[0]
    assert approval.id == requested.id
    assert approval.tenant_id == TENANT_A
    assert approval.job_id == job.id
    assert approval.pending_action == _action().model_dump(mode="json")
    assert approval.tenant_config_sha256 == job.tenant_config_sha256
    assert approval.expires_at == now + timedelta(hours=24)
    assert len(session.events) == 1
    event = session.events[0]
    assert event.event_type == "approval_requested"
    assert event.tenant_id == TENANT_A
    assert event.approval_id == approval.id
    assert event.payload == {
        "action": "create_case",
        "policy_reason": "approval_required",
    }
    assert len(session.queries) == 2
    assert all("FOR UPDATE" in query.upper() for query in session.queries)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tenant_id", "attempt_count"),
    [(TENANT_B, 2), (TENANT_A, 99)],
    ids=["other_tenant", "wrong_attempt"],
)
async def test_create_for_running_job_rejects_wrong_tenant_or_attempt(
    tenant_id: UUID,
    attempt_count: int,
) -> None:
    job = _job()
    session = _ApprovalSession([job])

    with pytest.raises(ApprovalCreationConflict):
        await ApprovalRepository(session).create_for_running_job(
            tenant_id,
            job.id,
            attempt_count=attempt_count,
            action=_action(),
            policy_reason="approval_required",
            expires_in=timedelta(hours=24),
            now=datetime(2026, 9, 23, 13, tzinfo=timezone.utc),
        )

    assert job.status == "running"
    assert session.approvals == []
    assert session.events == []
    expected_queries = 1 if tenant_id != TENANT_A else 2
    assert len(session.queries) == expected_queries
    assert all("FOR UPDATE" in query.upper() for query in session.queries)


@pytest.mark.asyncio
async def test_create_for_running_job_rejects_legacy_approval_without_phase8_command() -> None:
    job = _job()
    legacy = Approval(
        id=uuid4(),
        tenant_id=TENANT_A,
        job_id=job.id,
        case_id=None,
        action="create_case",
        status="pending",
        decision={},
    )
    session = _ApprovalSession([job], [legacy])

    with pytest.raises(ApprovalCreationConflict):
        await ApprovalRepository(session).create_for_running_job(
            TENANT_A,
            job.id,
            attempt_count=job.attempt_count,
            action=_action(),
            policy_reason="approval_required",
            expires_in=timedelta(hours=24),
            now=datetime(2026, 9, 23, 13, tzinfo=timezone.utc),
        )

    assert job.status == "running"
    assert session.approvals == [legacy]
    assert session.events == []


@pytest.mark.asyncio
async def test_duplicate_job_approval_returns_existing_command_without_new_event() -> None:
    job = _job()
    existing = Approval(
        id=uuid4(),
        tenant_id=TENANT_A,
        job_id=job.id,
        case_id=None,
        action="create_case",
        status="pending",
        decision={},
        policy_reason="approval_required",
        pending_action=_action().model_dump(mode="json"),
        tenant_config_sha256=job.tenant_config_sha256,
        expires_at=datetime(2026, 9, 24, 13, tzinfo=timezone.utc),
    )
    event = ApprovalEvent(
        id=uuid4(),
        tenant_id=TENANT_A,
        approval_id=existing.id,
        event_type="approval_requested",
        payload={"action": "create_case"},
    )
    session = _ApprovalSession([job], [existing])
    session.events.append(event)

    requested = await ApprovalRepository(session).create_for_running_job(
        TENANT_A,
        job.id,
        attempt_count=job.attempt_count,
        action=_action(),
        policy_reason="approval_required",
        expires_in=timedelta(hours=24),
        now=datetime(2026, 9, 23, 13, tzinfo=timezone.utc),
    )

    assert requested.id == existing.id
    assert requested.expires_at == existing.expires_at
    assert session.approvals == [existing]
    assert session.events == [event]
    assert job.status == "running"


@pytest.mark.asyncio
async def test_postgres_approval_approve_create_case_is_atomic_and_idempotent(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A live approval creates one case and repeated approve cannot replay it."""
    tenant = Tenant(
        id=uuid4(),
        slug=f"approval-{uuid4().hex}",
        name="Approval tenant",
        status="active",
    )
    job = AgentJob(
        id=uuid4(),
        tenant_id=tenant.id,
        status="running",
        source_snapshot={
            "channel": "email",
            "subject": "Approved load",
            "body": "Create this case",
        },
        tenant_config_snapshot={"slug": "approval"},
        tenant_config_sha256="b" * 64,
        risk_signals={},
        attempt_count=1,
        available_at=DECISION_TIME,
    )
    idempotency = IdempotencyRecord(
        id=uuid4(),
        tenant_id=tenant.id,
        key=f"approval-{job.id}",
        request_hash="c" * 64,
        job_id=job.id,
        response={},
    )
    action = PendingAction(
        name="create_case",
        arguments={"customer_id": None},
        known_fields={"summary": "Approved case"},
    )

    async with postgres_session_factory() as session:
        session.add(tenant)
        await session.flush()
        session.add_all([job, idempotency])
        await session.commit()

    async with postgres_session_factory() as session:
        requested = await ApprovalRepository(session).create_for_running_job(
            tenant.id,
            job.id,
            attempt_count=job.attempt_count,
            action=action,
            policy_reason="approval_required",
            expires_in=timedelta(hours=24),
            now=DECISION_TIME,
        )

    operator = AuthenticatedOperator(
        tenant_id=tenant.id,
        actor_ref="ops-alice",
        credential_id=uuid4(),
    )
    request = ApprovalDecisionRequest(
        decision="approve",
        reason="Approved by operations",
    )
    first_clock = DECISION_TIME + timedelta(minutes=1)
    async with postgres_session_factory() as session:
        first = await ApprovalDecisionService(
            session,
            PostgresApprovalActionExecutor(),
            clock=lambda: first_clock,
        ).decide(requested.id, operator, request)

    async with postgres_session_factory() as session:
        approval = await session.get(Approval, requested.id)
        persisted_job = await session.get(AgentJob, job.id)
        persisted_idempotency = await session.scalar(
            select(IdempotencyRecord).where(IdempotencyRecord.job_id == job.id)
        )
        cases = (
            await session.scalars(
                select(IntakeCase).where(IntakeCase.tenant_id == tenant.id)
            )
        ).all()
        case_events = (
            await session.scalars(
                select(CaseEvent).where(CaseEvent.tenant_id == tenant.id)
            )
        ).all()
        approval_events = (
            await session.scalars(
                select(ApprovalEvent)
                .where(ApprovalEvent.tenant_id == tenant.id)
                .order_by(ApprovalEvent.created_at, ApprovalEvent.id)
            )
        ).all()

    assert first.status == "approved"
    assert first.executed is True
    assert len(cases) == 1
    assert len(case_events) == 1
    assert case_events[0].event_type == "created"
    assert approval is not None
    assert approval.case_id == cases[0].id
    assert persisted_job is not None
    assert persisted_job.side_effect_committed_at is not None
    assert persisted_idempotency is not None
    assert persisted_idempotency.case_id == cases[0].id
    assert len(approval_events) == 3
    assert {event.event_type for event in approval_events} == {
        "approval_requested",
        "approval_approved",
        "approved_action_executed",
    }

    async with postgres_session_factory() as session:
        repeated = await ApprovalDecisionService(
            session,
            PostgresApprovalActionExecutor(),
            clock=lambda: first_clock + timedelta(minutes=1),
        ).decide(requested.id, operator, request)

    async with postgres_session_factory() as session:
        case_count = await session.scalar(
            select(func.count())
            .select_from(IntakeCase)
            .where(IntakeCase.tenant_id == tenant.id)
        )
        approval_event_count = await session.scalar(
            select(func.count())
            .select_from(ApprovalEvent)
            .where(ApprovalEvent.approval_id == requested.id)
        )

    assert repeated == first
    assert case_count == 1
    assert approval_event_count == 3
