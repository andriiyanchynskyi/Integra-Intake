"""Run claimed agent jobs outside the FastAPI request lifecycle.

The worker owns the async event loop and every SQLAlchemy session.  A claimed
job is handed to a synchronous thread only after its lease transaction has
committed; synchronous tool calls use ``WorkerAsyncGateway`` to schedule
coroutines back on this owner loop.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from app.agent.models import AgentRunResult, RunStatus
from app.core.config import Settings, settings
from app.domain.job_repository import ClaimedJob, JobRepository
from app.runtime.factory import AgentRuntimeFactory
from app.runtime.gateway import WorkerAsyncGateway


RETRY_DELAYS_SECONDS = (0.5, 1.0, 2.0, 4.0)


class RetryableJobError(RuntimeError):
    """A stable, safe-to-retry worker failure.

    The exception message is for local diagnostics only and is never persisted.
    ``error_code`` is intentionally selected from a small stable contract so
    retry rows cannot become an exception-text log sink.
    """

    def __init__(
        self,
        message: str = "provider unavailable",
        *,
        error_code: str = "provider_unavailable",
        side_effect_committed: bool = False,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.side_effect_committed = side_effect_committed


# A descriptive alias keeps callers that use the runtime terminology stable.
WorkerRetryableError = RetryableJobError


class AgentWorker:
    """Lease, execute, and finalize one job at a time on an owned loop."""

    def __init__(
        self,
        session_factory: Any,
        runtime_factory: Any | None = None,
        runtime_settings: Settings | Any | None = None,
        *,
        settings: Settings | Any | None = None,
        clock: Callable[[], datetime] | None = None,
        to_thread: Callable[..., Awaitable[Any]] | None = None,
        sleep: Callable[[float], Awaitable[Any]] | None = None,
        random_uniform: Callable[[float, float], float] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings or runtime_settings or Settings()
        self._runtime_factory = runtime_factory or AgentRuntimeFactory(
            session_factory,
            runtime_settings=self._settings,
        )
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._to_thread = to_thread or asyncio.to_thread
        self._sleep = sleep or asyncio.sleep
        self._random_uniform = random_uniform or random.uniform

    @classmethod
    def from_settings(cls, runtime_settings: Settings = settings) -> "AgentWorker":
        """Construct the process worker without importing it from FastAPI."""

        from app.db.session import async_session_factory

        runtime_factory = AgentRuntimeFactory(
            async_session_factory,
            runtime_settings=runtime_settings,
        )
        return cls(
            async_session_factory,
            runtime_factory=runtime_factory,
            runtime_settings=runtime_settings,
        )

    async def serve_once(self) -> bool:
        """Recover leases, claim at most one job, and process it."""

        now = self._now()
        async with self._session_factory() as session:
            repository = JobRepository(session)
            await repository.recover_expired_leases(
                now=now,
                max_retries=self._max_retries,
                retry_delay=timedelta(seconds=self._retry_delay_with_jitter(1)),
            )
            claimed = await repository.claim_next(
                now=now,
                lease_until=now + timedelta(seconds=self._lease_seconds),
            )

        if claimed is None:
            return False

        gateway = WorkerAsyncGateway(asyncio.get_running_loop())
        try:
            result = await self._to_thread(
                self._run_claimed_job,
                claimed,
                gateway,
            )
        except RetryableJobError as exc:
            if exc.side_effect_committed or await self._has_side_effect(claimed):
                await self._mark_failed_uncertain(
                    claimed,
                    "side_effect_committed",
                )
            elif claimed.attempt_count > self._max_retries:
                await self._mark_failed(claimed, "retry_exhausted")
            else:
                await self._schedule_retry(claimed, exc.error_code)
        except Exception:
            if await self._has_side_effect(claimed):
                await self._mark_failed_uncertain(
                    claimed,
                    "unexpected_after_side_effect",
                )
            else:
                await self._mark_failed(claimed, "worker_error")
        else:
            await self._persist_result(claimed, result)
        return True

    async def serve(self) -> None:
        """Run until cancelled, polling only when no job was available."""

        while True:
            claimed = await self.serve_once()
            if not claimed:
                await self._sleep(self._poll_interval_seconds)

    def _run_claimed_job(
        self,
        claimed: ClaimedJob,
        gateway: WorkerAsyncGateway,
    ) -> AgentRunResult:
        runtime = self._runtime_factory.build(claimed, gateway)
        try:
            try:
                return runtime.loop.run(runtime.initial_messages)
            except (httpx.TimeoutException, httpx.NetworkError) as error:
                raise RetryableJobError(error_code="provider_unavailable") from error
            except httpx.HTTPStatusError as error:
                status_code = error.response.status_code
                if status_code == 408 or status_code == 429 or status_code >= 500:
                    raise RetryableJobError(
                        error_code="provider_unavailable"
                    ) from error
                raise
        finally:
            runtime.close()

    async def _persist_result(
        self,
        claimed: ClaimedJob,
        result: AgentRunResult,
    ) -> None:
        now = self._now()
        if result.status != RunStatus.COMPLETED:
            uncertain = await self._has_side_effect(claimed)
            if uncertain:
                async with self._session_factory() as session:
                    repository = JobRepository(session)
                    await repository.mark_failed_uncertain(
                        claimed.id,
                        "agent_failed_after_side_effect",
                        now=now,
                        tenant_id=claimed.tenant_id,
                        attempt_count=claimed.attempt_count,
                    )
                return
        async with self._session_factory() as session:
            repository = JobRepository(session)
            if result.status == RunStatus.COMPLETED:
                await repository.mark_succeeded(
                    claimed.id,
                    self._result_summary(result),
                    now=now,
                    tenant_id=claimed.tenant_id,
                    attempt_count=claimed.attempt_count,
                )
            else:
                await repository.mark_failed(
                    claimed.id,
                    "agent_failed",
                    now=now,
                    tenant_id=claimed.tenant_id,
                    attempt_count=claimed.attempt_count,
                )

    async def _schedule_retry(
        self,
        claimed: ClaimedJob,
        error_code: str,
    ) -> None:
        now = self._now()
        delay = self._retry_delay_with_jitter(claimed.attempt_count)
        async with self._session_factory() as session:
            repository = JobRepository(session)
            await repository.schedule_retry(
                claimed.id,
                error_code,
                available_at=now + timedelta(seconds=delay),
                tenant_id=claimed.tenant_id,
                attempt_count=claimed.attempt_count,
            )

    async def _mark_failed(
        self,
        claimed: ClaimedJob,
        error_code: str,
    ) -> None:
        async with self._session_factory() as session:
            repository = JobRepository(session)
            await repository.mark_failed(
                claimed.id,
                error_code,
                now=self._now(),
                tenant_id=claimed.tenant_id,
                attempt_count=claimed.attempt_count,
            )

    async def _mark_failed_uncertain(
        self,
        claimed: ClaimedJob,
        error_code: str,
    ) -> None:
        async with self._session_factory() as session:
            repository = JobRepository(session)
            await repository.mark_failed_uncertain(
                claimed.id,
                error_code,
                now=self._now(),
                tenant_id=claimed.tenant_id,
                attempt_count=claimed.attempt_count,
            )

    async def _has_side_effect(self, claimed: ClaimedJob) -> bool:
        if claimed.side_effect_committed_at is not None:
            return True
        async with self._session_factory() as session:
            repository = JobRepository(session)
            marker_reader = getattr(repository, "get_side_effect_marker", None)
            if marker_reader is None:
                return False
            marker = await marker_reader(
                claimed.id,
                tenant_id=claimed.tenant_id,
            )
            return marker is not None

    @staticmethod
    def _result_summary(result: AgentRunResult) -> dict[str, object]:
        return {
            "status": result.status.value,
            "reason": result.reason.value,
            "steps": result.steps,
            "final_response": result.final_response,
        }

    def _retry_delay(self, attempt_count: int) -> float:
        index = max(0, min(attempt_count - 1, len(RETRY_DELAYS_SECONDS) - 1))
        return RETRY_DELAYS_SECONDS[index]

    def _retry_delay_with_jitter(self, attempt_count: int) -> float:
        delay = self._retry_delay(attempt_count)
        return delay + self._random_uniform(0.0, delay * 0.1)

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value

    @property
    def _poll_interval_seconds(self) -> float:
        return float(self._settings.worker_poll_interval_seconds)

    @property
    def _lease_seconds(self) -> int:
        return int(self._settings.worker_lease_seconds)

    @property
    def _max_retries(self) -> int:
        return int(self._settings.worker_max_retries)


__all__ = [
    "AgentWorker",
    "RetryableJobError",
    "WorkerRetryableError",
]
