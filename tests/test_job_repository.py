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
from app.domain.job_repository import JobRepository


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

    assert recovered == 1
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
