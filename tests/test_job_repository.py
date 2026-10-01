"""Offline lifecycle tests for the Phase-7 intake-job repository."""

from __future__ import annotations

from collections.abc import Iterable
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from app.db.models import AgentJob
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

    def __init__(self, jobs: Iterable[AgentJob]) -> None:
        self.jobs = list(jobs)
        self.queries: list[str] = []

    def begin(self) -> _Transaction:
        return _Transaction()

    def add(self, value: AgentJob) -> None:
        if value not in self.jobs:
            self.jobs.append(value)

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        return None

    async def execute(self, statement: object) -> _Result:
        compiled = statement.compile(dialect=postgresql.dialect())
        self.queries.append(str(compiled))
        params = compiled.params
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


class _ReadSession:
    def __init__(self, row: tuple[object, ...]) -> None:
        self.row = row
        self.queries: list[str] = []

    async def execute(self, statement: object) -> _ReadResult:
        compiled = statement.compile(dialect=postgresql.dialect())
        self.queries.append(str(compiled))
        return _ReadResult(self.row)


class _TransitionResult:
    def __init__(self, job: AgentJob | None) -> None:
        self.job = job

    def scalar_one_or_none(self) -> AgentJob | None:
        return self.job


class _TransitionSession:
    """Session double that enforces the repository's ownership predicates."""

    def __init__(self, job: AgentJob) -> None:
        self.job = job
        self.queries: list[str] = []

    def begin(self) -> _Transaction:
        return _Transaction()

    async def execute(self, statement: object) -> _TransitionResult:
        compiled = statement.compile(dialect=postgresql.dialect())
        self.queries.append(str(compiled))
        params = compiled.params
        uuid_values = [value for value in params.values() if hasattr(value, "hex")]
        attempt_values = [
            value for value in params.values() if isinstance(value, int)
        ]
        job_id_matches = not uuid_values or uuid_values[0] == self.job.id
        tenant_matches = len(uuid_values) < 2 or uuid_values[1] == self.job.tenant_id
        attempt_matches = not attempt_values or attempt_values[-1] == self.job.attempt_count
        status_matches = self.job.status == "running"
        return _TransitionResult(
            self.job
            if job_id_matches and tenant_matches and attempt_matches and status_matches
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
    session = _Session([expired])

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


@pytest.mark.asyncio
async def test_expired_lease_with_marker_becomes_failed_uncertain_and_is_not_requeued() -> None:
    now = datetime(2026, 9, 21, tzinfo=timezone.utc)
    marked = _job(
        status="running",
        attempt_count=1,
        lease_expires_at=now - timedelta(seconds=1),
        side_effect_committed_at=now - timedelta(seconds=2),
    )
    session = _Session([marked])

    await JobRepository(session).recover_expired_leases(
        now=now,
        max_retries=4,
        retry_delay=timedelta(seconds=2),
    )

    assert marked.status == "failed_uncertain"
    assert marked.error_code
    assert marked.available_at != now + timedelta(seconds=2)


@pytest.mark.asyncio
async def test_expired_lease_at_retry_budget_is_failed() -> None:
    now = datetime(2026, 9, 21, tzinfo=timezone.utc)
    exhausted = _job(
        status="running",
        attempt_count=5,
        lease_expires_at=now - timedelta(seconds=1),
    )
    session = _Session([exhausted])

    await JobRepository(session).recover_expired_leases(
        now=now,
        max_retries=4,
        retry_delay=timedelta(seconds=2),
    )

    assert exhausted.status == "failed"
    assert exhausted.error_code == "retry_exhausted"
    assert exhausted.finished_at == now


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
