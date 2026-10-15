"""Offline lifecycle tests for the Phase-7 intake-job repository."""

from __future__ import annotations

from collections.abc import Iterable
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from app.db.models import AgentJob, Approval, ApprovalEvent
from app.domain.job_repository import JobLeaseLostError, JobRepository
from app.runtime.profiles import TenantProfileResolver


def _job(
    *,
    status: str = "queued",
    available_at: datetime | None = None,
    attempt_count: int = 0,
    lease_expires_at: datetime | None = None,
    side_effect_committed_at: datetime | None = None,
) -> AgentJob:
    now = datetime.now(timezone.utc)
    return AgentJob(
        id=uuid4(),
        tenant_id=uuid4(),
        trace_id=uuid4(),
        created_at=now - timedelta(seconds=5),
        status=status,
        source_snapshot={"channel": "email", "subject": "Load", "body": "Need a truck"},
        tenant_config_snapshot={"display_name": "test"},
        tenant_config_sha256="a" * 64,
        risk_signals={"safety_or_legal_risk": False},
        attempt_count=attempt_count,
        available_at=available_at or now,
        lease_expires_at=lease_expires_at,
        side_effect_committed_at=side_effect_committed_at,
    )


class _ScalarRows:
    def __init__(self, rows: Iterable[AgentJob]) -> None:
        self._rows = list(rows)

    def all(self) -> list[AgentJob]:
        return list(self._rows)

    def first(self) -> AgentJob | None:
        return self._rows[0] if self._rows else None


class _Result:
    def __init__(self, rows: Iterable[AgentJob]) -> None:
        self._rows = list(rows)

    def one_or_none(self) -> tuple[object, ...] | None:
        if not self._rows:
            return None
        job = self._rows[0]
        return (
            job.trace_id,
            job.tenant_config_snapshot,
            job.tenant_config_sha256,
        )

    def scalar_one_or_none(self) -> AgentJob | None:
        return self._rows[0] if self._rows else None

    def scalars(self) -> _ScalarRows:
        return _ScalarRows(self._rows)


class _Transaction(AbstractAsyncContextManager["_Transaction"]):
    async def __aenter__(self) -> "_Transaction":
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None


class _Session:
    """Small AsyncSession double that evaluates the repository's job filters."""

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

    def add(self, value: AgentJob | ApprovalEvent) -> None:
        if isinstance(value, ApprovalEvent):
            self.events.append(value)
        elif value not in self.jobs:
            self.jobs.append(value)

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        return None

    async def execute(self, statement: object) -> _Result:
        compiled = statement.compile(dialect=postgresql.dialect())
        self.queries.append(str(compiled))
        params = compiled.params
        if "FROM approvals" in str(compiled):
            job_ids = {job.id for job in self.jobs}
            tenant_ids = {job.tenant_id for job in self.jobs}
            job_id = next(
                (value for value in params.values() if value in job_ids),
                None,
            )
            tenant_id = next(
                (value for value in params.values() if value in tenant_ids),
                None,
            )
            return _Result(
                approval
                for approval in self.approvals
                if approval.job_id == job_id
                and approval.tenant_id == tenant_id
                and approval.status == "staged"
            )
        status = next(
            (value for value in params.values() if value in {"queued", "running"}),
            None,
        )
        now = next(
            (
                value
                for value in params.values()
                if isinstance(value, datetime)
            ),
            None,
        )
        rows = self.jobs
        if status == "queued":
            rows = [
                job
                for job in rows
                if job.status == "queued"
                and (now is None or job.available_at <= now)
            ]
        elif status == "running":
            rows = [
                job
                for job in rows
                if job.status == "running"
                and job.lease_expires_at is not None
                and (now is None or job.lease_expires_at <= now)
            ]
        return _Result(rows)


def _staged_approval(job: AgentJob) -> Approval:
    return Approval(
        id=uuid4(),
        tenant_id=job.tenant_id,
        job_id=job.id,
        action="create_reply_draft",
        status="staged",
        decision={},
    )


@pytest.mark.asyncio
async def test_claim_next_claims_only_due_queued_job_and_leases_it() -> None:
    now = datetime(2026, 9, 21, tzinfo=timezone.utc)
    due = _job(available_at=now - timedelta(seconds=1))
    future = _job(available_at=now + timedelta(seconds=30))
    running = _job(status="running", lease_expires_at=now + timedelta(minutes=1))
    session = _Session([future, running, due])

    claimed = await JobRepository(session).claim_next(
        now=now,
        lease_until=now + timedelta(minutes=1),
    )

    assert claimed is not None
    assert claimed.id == due.id
    assert claimed.trace_id == due.trace_id
    assert claimed.created_at == due.created_at
    assert claimed.available_at == due.available_at
    assert claimed.started_at == now
    assert due.status == "running"
    assert due.attempt_count == 1
    assert due.started_at == now
    assert due.lease_expires_at == now + timedelta(minutes=1)
    assert any("FOR UPDATE SKIP LOCKED" in query.upper() for query in session.queries)


@pytest.mark.asyncio
async def test_claim_next_does_not_claim_nonexpired_running_job() -> None:
    now = datetime(2026, 9, 21, tzinfo=timezone.utc)
    running = _job(status="running", lease_expires_at=now + timedelta(minutes=1))
    session = _Session([running])

    assert (
        await JobRepository(session).claim_next(
            now=now,
            lease_until=now + timedelta(minutes=1),
        )
        is None
    )
    assert running.attempt_count == 0
    assert running.status == "running"


class _ReadResult:
    def __init__(self, row: tuple[object, ...] | None) -> None:
        self.row = row

    def one_or_none(self) -> tuple[object, ...] | None:
        return self.row

    def scalar_one_or_none(self) -> object | None:
        if self.row is None:
            return None
        if isinstance(self.row, tuple) and len(self.row) == 1:
            return self.row[0]
        return None

    def all(self) -> list[tuple[object, ...]]:
        return []


class _ReadSession:
    def __init__(self, row: tuple[object, ...]) -> None:
        self.row = row
        self.queries: list[str] = []

    async def execute(self, statement: object) -> _ReadResult:
        compiled = statement.compile(dialect=postgresql.dialect())
        self.queries.append(str(compiled))
        query = str(compiled)
        if "FROM idempotency_records" in query:
            return _ReadResult(None)
        if "FROM job_artifacts" in query:
            return _ReadResult(None)
        return _ReadResult(self.row)


class _TransitionResult:
    def __init__(self, value: AgentJob | Approval | None) -> None:
        self.value = value

    def scalar_one_or_none(self) -> AgentJob | Approval | None:
        return self.value


class _TransitionSession:
    """Session double that enforces the repository's ownership predicates."""

    def __init__(self, job: AgentJob, approval: Approval | None = None) -> None:
        self.job = job
        self.approval = approval
        self.events: list[ApprovalEvent] = []
        self.queries: list[str] = []

    def begin(self) -> _Transaction:
        return _Transaction()

    def add(self, value: ApprovalEvent) -> None:
        self.events.append(value)

    async def execute(self, statement: object) -> _TransitionResult:
        compiled = statement.compile(dialect=postgresql.dialect())
        self.queries.append(str(compiled))
        params = compiled.params
        if "FROM approvals" in str(compiled):
            return _TransitionResult(
                self.approval
                if self.approval is not None
                and self.approval.job_id == self.job.id
                and self.approval.tenant_id == self.job.tenant_id
                else None
            )
        uuid_values = [value for value in params.values() if hasattr(value, "hex")]
        attempt_values = [
            value for value in params.values() if isinstance(value, int)
        ]
        job_id_matches = not uuid_values or uuid_values[0] == self.job.id
        tenant_matches = len(uuid_values) < 2 or uuid_values[1] == self.job.tenant_id
        attempt_matches = not attempt_values or attempt_values[-1] == self.job.attempt_count
        return _TransitionResult(
            self.job
            if job_id_matches and tenant_matches and attempt_matches
            else None
        )


@pytest.mark.asyncio
async def test_get_read_for_tenant_uses_job_and_tenant_predicates() -> None:
    profile = TenantProfileResolver("examples").resolve("freight-broker")
    now = datetime.now(timezone.utc)
    tenant_id = uuid4()
    job = AgentJob(
        id=uuid4(),
        tenant_id=tenant_id,
        trace_id=uuid4(),
        status="succeeded",
        source_snapshot={"channel": "web", "subject": "Synthetic", "body": "Synthetic"},
        tenant_config_snapshot=profile.snapshot,
        tenant_config_sha256=profile.sha256,
        risk_signals={},
        attempt_count=1,
        created_at=now,
        started_at=now,
        finished_at=now,
        result={"routing_status": "ready"},
        error_code=None,
    )
    approval_id = uuid4()
    session = _ReadSession((job, approval_id, "pending"))

    read = await JobRepository(session).get_read_for_tenant(job.id, tenant_id)

    assert read is not None
    assert read.job_id == job.id
    assert read.tenant_config_snapshot == profile.snapshot
    assert read.approval_id == approval_id
    assert any("agent_jobs.id" in query and "agent_jobs.tenant_id" in query for query in session.queries)


@pytest.mark.asyncio
async def test_expired_unmarked_lease_is_requeued_with_bounded_delay() -> None:
    now = datetime(2026, 9, 21, tzinfo=timezone.utc)
    expired = _job(
        status="running",
        attempt_count=1,
        lease_expires_at=now - timedelta(seconds=1),
    )
    approval = _staged_approval(expired)
    session = _Session([expired], [approval])

    recovered = await JobRepository(session).recover_expired_leases(
        now=now,
        max_retries=4,
        retry_delay=timedelta(seconds=2),
    )

    assert len(recovered) == 1
    assert recovered[0].trace_id == expired.trace_id
    assert recovered[0].status == "queued"
    assert recovered[0].error_code == "lease_expired"
    assert expired.status == "queued"
    assert expired.available_at == now + timedelta(seconds=2)
    assert expired.lease_expires_at is None
    assert expired.error_code == "lease_expired"
    assert approval.status == "staged"
    assert session.events == []


@pytest.mark.asyncio
async def test_expired_lease_with_marker_becomes_failed_uncertain_and_is_not_requeued() -> None:
    now = datetime(2026, 9, 21, tzinfo=timezone.utc)
    marked = _job(
        status="running",
        attempt_count=1,
        lease_expires_at=now - timedelta(seconds=1),
        side_effect_committed_at=now - timedelta(seconds=2),
    )
    approval = _staged_approval(marked)
    session = _Session([marked], [approval])

    await JobRepository(session).recover_expired_leases(
        now=now,
        max_retries=4,
        retry_delay=timedelta(seconds=2),
    )

    assert marked.status == "failed_uncertain"
    assert marked.error_code
    assert marked.available_at != now + timedelta(seconds=2)
    assert approval.status == "cancelled"
    assert [event.event_type for event in session.events] == ["approval_cancelled"]


@pytest.mark.asyncio
async def test_expired_lease_at_retry_budget_is_failed() -> None:
    now = datetime(2026, 9, 21, tzinfo=timezone.utc)
    exhausted = _job(
        status="running",
        attempt_count=5,
        lease_expires_at=now - timedelta(seconds=1),
    )
    approval = _staged_approval(exhausted)
    session = _Session([exhausted], [approval])

    await JobRepository(session).recover_expired_leases(
        now=now,
        max_retries=4,
        retry_delay=timedelta(seconds=2),
    )

    assert exhausted.status == "failed"
    assert exhausted.error_code == "retry_exhausted"
    assert exhausted.finished_at == now
    assert approval.status == "cancelled"
    assert [event.event_type for event in session.events] == ["approval_cancelled"]


@pytest.mark.asyncio
async def test_get_observation_identity_is_tenant_scoped_and_copies_profile_identity() -> None:
    job = _job()
    session = _Session([job])

    identity = await JobRepository(session).get_observation_identity(
        job.id,
        tenant_id=job.tenant_id,
    )

    assert identity is not None
    assert identity.trace_id == job.trace_id
    assert identity.tenant_config_snapshot == job.tenant_config_snapshot
    assert identity.tenant_config_snapshot is not job.tenant_config_snapshot
    assert identity.tenant_config_sha256 == job.tenant_config_sha256
    query = session.queries[-1]
    assert "agent_jobs.tenant_id" in query
    assert "agent_jobs.id" in query


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tenant_id", "attempt_count"),
    [(uuid4(), 1), (None, 2)],
)
async def test_terminal_transition_rejects_stale_tenant_or_attempt(
    tenant_id: object,
    attempt_count: int,
) -> None:
    job = _job(status="running", attempt_count=1)
    session = _TransitionSession(job)
    repository = JobRepository(session)

    with pytest.raises(JobLeaseLostError):
        await repository.mark_succeeded(
            job.id,
            {"status": "completed"},
            now=datetime.now(timezone.utc),
            tenant_id=tenant_id if tenant_id is not None else job.tenant_id,
            attempt_count=attempt_count,
        )

    assert job.status == "running"
    assert job.result is None


@pytest.mark.asyncio
async def test_terminal_transition_accepts_owned_running_attempt() -> None:
    job = _job(status="running", attempt_count=2)
    session = _TransitionSession(job)

    await JobRepository(session).mark_succeeded(
        job.id,
        {"status": "completed"},
        now=datetime.now(timezone.utc),
        tenant_id=job.tenant_id,
        attempt_count=job.attempt_count,
    )

    assert job.status == "succeeded"
    assert job.result == {"status": "completed"}


@pytest.mark.asyncio
async def test_record_awaiting_approval_result_atomically_parks_running_job(
) -> None:
    started_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    stale_finished_at = datetime.now(timezone.utc) - timedelta(seconds=2)
    lease_expires_at = datetime.now(timezone.utc) + timedelta(minutes=1)
    job = _job(
        status="running",
        attempt_count=2,
        lease_expires_at=lease_expires_at,
    )
    job.started_at = started_at
    job.finished_at = stale_finished_at
    job.error_code = "stale_error"
    approval = Approval(
        id=uuid4(),
        tenant_id=job.tenant_id,
        job_id=job.id,
        action="create_reply_draft",
        status="staged",
        decision={},
    )
    summary = {
        "status": "completed",
        "reason": "executor_stopped",
        "steps": 1,
        "execution_path": [
            {
                "step": 1,
                "action_key": "create_reply_draft",
                "action_known": True,
                "policy_decision": "needs_approval",
                "routing_status": "urgent",
                "policy_reason": "approval_required",
                "missing_required_fields": [],
                "tool_outcome": None,
                "side_effect_committed": False,
            }
        ],
    }

    await JobRepository(
        _TransitionSession(job, approval)
    ).record_awaiting_approval_result(
        job.id,
        summary,
        tenant_id=job.tenant_id,
        attempt_count=job.attempt_count,
    )

    assert job.status == "awaiting_approval"
    assert job.started_at == started_at
    assert job.finished_at is None
    assert job.lease_expires_at is None
    assert job.error_code is None
    assert job.result == summary
    assert job.result is not summary
    assert approval.status == "pending"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "tenant_id", "attempt_count", "approval_status"),
    [
        ("awaiting_approval", None, 2, "staged"),
        ("running", uuid4(), 2, "staged"),
        ("running", None, 1, "staged"),
        ("running", None, 2, "pending"),
    ],
)
async def test_record_awaiting_approval_result_rejects_unowned_or_unparked_job(
    status: str,
    tenant_id: object,
    attempt_count: int,
    approval_status: str,
) -> None:
    job = _job(status=status, attempt_count=2)
    original_result = {"status": status}
    job.result = original_result
    approval = Approval(
        id=uuid4(),
        tenant_id=job.tenant_id,
        job_id=job.id,
        action="create_reply_draft",
        status=approval_status,
        decision={},
    )
    repository = JobRepository(_TransitionSession(job, approval))

    with pytest.raises(JobLeaseLostError):
        await repository.record_awaiting_approval_result(
            job.id,
            {"status": "completed", "execution_path": []},
            tenant_id=tenant_id if tenant_id is not None else job.tenant_id,
            attempt_count=attempt_count,
        )

    assert job.status == status
    assert job.result is original_result
    assert approval.status == approval_status


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_status", ["succeeded", "failed"])
async def test_terminal_transition_cancels_staged_approval(
    terminal_status: str,
) -> None:
    job = _job(status="running", attempt_count=2)
    approval = Approval(
        id=uuid4(),
        tenant_id=job.tenant_id,
        job_id=job.id,
        action="create_reply_draft",
        status="staged",
        decision={},
    )
    session = _TransitionSession(job, approval)
    repository = JobRepository(session)
    now = datetime.now(timezone.utc)

    if terminal_status == "succeeded":
        await repository.mark_succeeded(
            job.id,
            {"status": "completed"},
            now=now,
            tenant_id=job.tenant_id,
            attempt_count=job.attempt_count,
        )
    else:
        await repository.mark_failed(
            job.id,
            "agent_failed",
            now=now,
            tenant_id=job.tenant_id,
            attempt_count=job.attempt_count,
        )

    assert job.status == terminal_status
    assert approval.status == "cancelled"
    assert len(session.events) == 1
    event = session.events[0]
    assert event.tenant_id == job.tenant_id
    assert event.approval_id == approval.id
    assert event.event_type == "approval_cancelled"
    assert event.actor_ref is None
    assert event.payload == {}


@pytest.mark.asyncio
async def test_retry_and_side_effect_reads_require_owned_attempt() -> None:
    job = _job(status="running", attempt_count=3)
    session = _TransitionSession(job)
    repository = JobRepository(session)

    with pytest.raises(JobLeaseLostError):
        await repository.schedule_retry(
            job.id,
            "provider_unavailable",
            available_at=datetime.now(timezone.utc),
            tenant_id=job.tenant_id,
            attempt_count=2,
        )
    with pytest.raises(JobLeaseLostError):
        await repository.get_side_effect_marker(
            job.id,
            tenant_id=job.tenant_id,
            attempt_count=2,
        )

    assert job.status == "running"


@pytest.mark.asyncio
async def test_renew_lease_only_extends_owned_running_attempt() -> None:
    now = datetime.now(timezone.utc)
    job = _job(status="running", attempt_count=4, lease_expires_at=now)
    session = _TransitionSession(job)
    repository = JobRepository(session)
    lease_until = now + timedelta(minutes=1)

    assert (
        await repository.renew_lease(
            job.id,
            tenant_id=job.tenant_id,
            attempt_count=job.attempt_count,
            lease_until=lease_until,
        )
        is True
    )
    assert job.lease_expires_at == lease_until

    assert (
        await repository.renew_lease(
            job.id,
            tenant_id=job.tenant_id,
            attempt_count=job.attempt_count - 1,
            lease_until=lease_until + timedelta(minutes=1),
        )
        is False
    )
    assert job.lease_expires_at == lease_until
