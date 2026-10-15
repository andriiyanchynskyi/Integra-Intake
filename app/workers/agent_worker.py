"""Run claimed agent jobs outside the FastAPI request lifecycle.

The worker owns the async event loop and every SQLAlchemy session.  A claimed
job is handed to a synchronous thread only after its lease transaction has
committed; synchronous tool calls use ``WorkerAsyncGateway`` to schedule
coroutines back on this owner loop.
"""

from __future__ import annotations

import asyncio
import random
import inspect
import threading
import time
from collections.abc import Awaitable, Callable, Collection, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import httpx
from sqlalchemy.exc import DBAPIError

from app.agent.models import AgentRunResult, RunStatus, StopReason
from app.core.config import Settings, settings
from app.domain.approval_repository import ApprovalRepository, ExpiredApprovalObservation
from app.domain.job_repository import (
    ClaimedJob,
    JobLeaseLostError,
    JobRepository,
    LeaseRecoveryObservation,
)
from app.observability import (
    Component,
    EventLevel,
    EventName,
    NULL_OBSERVER,
    ObservationContext,
    ObservationEvent,
    Observer,
    OutcomeCode,
    PersistenceOperation,
    WorkerErrorCode,
    safe_emit,
)
from app.runtime.factory import AgentRuntimeFactory
from app.runtime.gateway import WorkerAsyncGateway
from app.runtime.preflight import TerminalPreflightResult
from app.runtime.profiles import resolve_persisted_profile


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


class ProviderRequestRejectedError(RuntimeError):
    """A provider rejected a request and retrying it unchanged cannot help."""


class TransientPersistenceError(RuntimeError):
    """A bounded persistence outage that the supervisor may retry."""


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
        heartbeat_wait: Callable[[float, asyncio.Event], Awaitable[bool]] | None = None,
        random_uniform: Callable[[float, float], float] | None = None,
        observer: Observer = NULL_OBSERVER,
        monotonic_clock: Callable[[], int] = time.perf_counter_ns,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings or runtime_settings or Settings()
        self._runtime_factory = runtime_factory or AgentRuntimeFactory(
            session_factory,
            runtime_settings=self._settings,
            observer=observer,
        )
        self._observer = observer
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._monotonic_clock = monotonic_clock
        self._to_thread = to_thread or asyncio.to_thread
        self._sleep = sleep or asyncio.sleep
        self._heartbeat_wait = heartbeat_wait or self._default_heartbeat_wait
        self._random_uniform = random_uniform or random.uniform

    @classmethod
    def from_settings(cls, runtime_settings: Settings = settings) -> "AgentWorker":
        """Construct the process worker without importing it from FastAPI."""

        from app.db.session import async_session_factory
        from app.observability import StructlogObserver

        observer = StructlogObserver()
        runtime_factory = AgentRuntimeFactory(
            async_session_factory,
            runtime_settings=runtime_settings,
            observer=observer,
        )
        return cls(
            async_session_factory,
            runtime_factory=runtime_factory,
            runtime_settings=runtime_settings,
            observer=observer,
        )

    async def serve_once(
        self,
        *,
        excluded_tenant_ids: Collection[UUID] = (),
        on_claim: Callable[[ClaimedJob], None] | None = None,
    ) -> bool:
        """Recover leases, claim at most one job, and process it."""

        now = self._now()
        async with self._session_factory() as session:
            approval_repository = ApprovalRepository(session)
            try:
                expired = await approval_repository.expire_due(now=now)
            except Exception:
                self._emit_persistence_failure(
                    trace_id=None,
                    tenant_id=None,
                    job_id=None,
                    operation=PersistenceOperation.DECIDE_APPROVAL,
                )
                raise
            if isinstance(expired, tuple):
                for observation in expired:
                    if isinstance(observation, ExpiredApprovalObservation):
                        self._emit_expired_approval(observation)
            repository = JobRepository(session)
            try:
                recovered = await repository.recover_expired_leases(
                    now=now,
                    max_retries=self._max_retries,
                    retry_delay=timedelta(seconds=self._retry_delay_with_jitter(1)),
                )
            except Exception:
                self._emit_persistence_failure(
                    trace_id=None,
                    tenant_id=None,
                    job_id=None,
                    operation=PersistenceOperation.RECOVER_LEASE,
                )
                raise
            if isinstance(recovered, tuple):
                for observation in recovered:
                    if isinstance(observation, LeaseRecoveryObservation):
                        self._emit_lease_recovered(observation)
            try:
                claimed = await repository.claim_next(
                    now=now,
                    lease_until=now + timedelta(seconds=self._lease_seconds),
                    excluded_tenant_ids=excluded_tenant_ids,
                )
            except Exception:
                self._emit_persistence_failure(
                    trace_id=None,
                    tenant_id=None,
                    job_id=None,
                    operation=PersistenceOperation.CLAIM_JOB,
                )
                raise

        if claimed is None:
            return False

        if on_claim is not None:
            on_claim(claimed)
        self._emit_job_claimed(claimed)
        self._emit_job_started(claimed)
        gateway = WorkerAsyncGateway(
            asyncio.get_running_loop(),
            timeout_seconds=self._gateway_timeout_seconds,
        )
        run_started_ns = self._monotonic_clock()
        finished = False
        try:
            try:
                result = await self._run_claimed_with_lease(claimed, gateway)
            except RetryableJobError as exc:
                run_duration_ms = self._elapsed_ms(run_started_ns)
                has_side_effect = exc.side_effect_committed or await self._has_side_effect(
                    claimed
                )
                if has_side_effect:
                    await self._mark_failed_uncertain(claimed, "side_effect_committed")
                    self._emit_job_finished(
                        claimed,
                        outcome=OutcomeCode.FAILED_UNCERTAIN,
                        duration_ms=run_duration_ms,
                        worker_error_code=WorkerErrorCode.SIDE_EFFECT_COMMITTED,
                        side_effect_committed=True,
                    )
                elif claimed.attempt_count > self._max_retries:
                    await self._mark_failed(claimed, "retry_exhausted")
                    self._emit_job_finished(
                        claimed,
                        outcome=OutcomeCode.FAILED,
                        duration_ms=run_duration_ms,
                        worker_error_code=WorkerErrorCode.RETRY_EXHAUSTED,
                    )
                else:
                    retry_delay = await self._schedule_retry(claimed, exc.error_code)
                    self._emit_retry_scheduled(
                        claimed,
                        error_code=exc.error_code,
                        retry_delay_ms=max(0, int(retry_delay * 1000)),
                    )
                    self._emit_job_finished(
                        claimed,
                        outcome=OutcomeCode.RETRY_SCHEDULED,
                        duration_ms=run_duration_ms,
                        worker_error_code=self._worker_error_code(exc.error_code),
                    )
                finished = True
            except ProviderRequestRejectedError:
                run_duration_ms = self._elapsed_ms(run_started_ns)
                has_side_effect = await self._has_side_effect(claimed)
                if has_side_effect:
                    await self._mark_failed_uncertain(
                        claimed,
                        "unexpected_after_side_effect",
                    )
                    self._emit_job_finished(
                        claimed,
                        outcome=OutcomeCode.FAILED_UNCERTAIN,
                        duration_ms=run_duration_ms,
                        worker_error_code=WorkerErrorCode.UNEXPECTED_AFTER_SIDE_EFFECT,
                        side_effect_committed=True,
                    )
                else:
                    await self._mark_failed(
                        claimed,
                        "provider_request_rejected",
                    )
                    self._emit_job_finished(
                        claimed,
                        outcome=OutcomeCode.FAILED,
                        duration_ms=run_duration_ms,
                        worker_error_code=WorkerErrorCode.PROVIDER_REQUEST_REJECTED,
                    )
                finished = True
            except JobLeaseLostError:
                self._emit_job_finished(
                    claimed,
                    outcome=OutcomeCode.LEASE_LOST,
                    duration_ms=self._elapsed_ms(run_started_ns),
                    worker_error_code=WorkerErrorCode.LEASE_LOST,
                )
                finished = True
                return True
            except Exception:
                run_duration_ms = self._elapsed_ms(run_started_ns)
                has_side_effect = await self._has_side_effect(claimed)
                if has_side_effect:
                    await self._mark_failed_uncertain(
                        claimed,
                        "unexpected_after_side_effect",
                    )
                    self._emit_job_finished(
                        claimed,
                        outcome=OutcomeCode.FAILED_UNCERTAIN,
                        duration_ms=run_duration_ms,
                        worker_error_code=WorkerErrorCode.UNEXPECTED_AFTER_SIDE_EFFECT,
                        side_effect_committed=True,
                    )
                else:
                    await self._mark_failed(claimed, "worker_error")
                    self._emit_job_finished(
                        claimed,
                        outcome=OutcomeCode.FAILED,
                        duration_ms=run_duration_ms,
                        worker_error_code=WorkerErrorCode.WORKER_ERROR,
                    )
                finished = True
            else:
                run_duration_ms = self._elapsed_ms(run_started_ns)
                if isinstance(result, TerminalPreflightResult):
                    outcome = await self._persist_preflight_result(claimed, result)
                    worker_error_code = (
                        WorkerErrorCode.CAPABILITY_UNAVAILABLE
                        if result.reason == "capability_unavailable"
                        else (
                            WorkerErrorCode.SNAPSHOT_INCOMPATIBLE
                            if result.reason == "snapshot_incompatible"
                            else None
                        )
                    )
                    self._emit_job_finished(
                        claimed,
                        outcome=outcome,
                        duration_ms=run_duration_ms,
                        steps=0,
                        worker_error_code=worker_error_code,
                    )
                    finished = True
                    return True
                outcome = await self._persist_result(claimed, result)
                self._emit_job_finished(
                    claimed,
                    outcome=outcome,
                    duration_ms=run_duration_ms,
                    steps=result.steps,
                    stop_reason=result.reason,
                    side_effect_committed=outcome
                    is OutcomeCode.FAILED_UNCERTAIN,
                )
                finished = True
        except JobLeaseLostError:
            self._emit_job_finished(
                claimed,
                outcome=OutcomeCode.LEASE_LOST,
                duration_ms=self._elapsed_ms(run_started_ns),
                worker_error_code=WorkerErrorCode.LEASE_LOST,
            )
            finished = True
            return True
        except Exception:
            if not finished:
                self._emit_job_finished(
                    claimed,
                    outcome=OutcomeCode.FAILED,
                    duration_ms=self._elapsed_ms(run_started_ns),
                    worker_error_code=WorkerErrorCode.PERSISTENCE_ERROR,
                )
            raise
        return True

    async def serve(self) -> None:
        """Run bounded, tenant-fair jobs while supervising persistence outages."""

        backoff = self._persistence_backoff_initial_seconds
        active_tenants: set[UUID] = set()
        tasks: set[asyncio.Task[bool]] = set()
        task_holders: dict[asyncio.Task[bool], list[UUID]] = {}

        async def cancel_tasks() -> None:
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            tasks.clear()
            for holder in task_holders.values():
                for tenant_id in holder:
                    active_tenants.discard(tenant_id)
            task_holders.clear()

        while True:
            try:
                while len(tasks) < self._worker_concurrency:
                    holder: list[UUID] = []
                    claimed_signal = asyncio.Event()

                    def reserve(
                        claimed: ClaimedJob,
                        *,
                        _holder: list[UUID] = holder,
                        _signal: asyncio.Event = claimed_signal,
                    ) -> None:
                        active_tenants.add(claimed.tenant_id)
                        _holder.append(claimed.tenant_id)
                        _signal.set()

                    task = asyncio.create_task(
                        self.serve_once(
                            excluded_tenant_ids=tuple(active_tenants),
                            on_claim=reserve,
                        )
                    )
                    tasks.add(task)
                    task_holders[task] = holder
                    while not claimed_signal.is_set() and not task.done():
                        await asyncio.sleep(0)
                    if task.done():
                        tasks.discard(task)
                        finished_holder = task_holders.pop(task, [])
                        for tenant_id in finished_holder:
                            active_tenants.discard(tenant_id)
                        result = task.result()
                        if not result:
                            break

                if not tasks:
                    await self._sleep(self._poll_interval_seconds)
                    continue
                done, _ = await asyncio.wait(
                    tasks,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in done:
                    tasks.discard(task)
                    holder = task_holders.pop(task, [])
                    for tenant_id in holder:
                        active_tenants.discard(tenant_id)
                    result = task.result()
                    if not result:
                        # A no-work slot is normal; the other active slots may
                        # still be processing jobs.
                        continue
            except asyncio.CancelledError:
                await cancel_tasks()
                raise
            except TransientPersistenceError:
                await cancel_tasks()
                delay = min(backoff, self._persistence_backoff_max_seconds)
                await self._sleep(
                    delay + self._random_uniform(0.0, delay * 0.1)
                )
                backoff = min(
                    self._persistence_backoff_max_seconds,
                    max(backoff * 2, self._persistence_backoff_initial_seconds),
                )
                continue
            except (DBAPIError, ConnectionError, TimeoutError) as error:
                await cancel_tasks()
                delay = min(backoff, self._persistence_backoff_max_seconds)
                await self._sleep(
                    delay + self._random_uniform(0.0, delay * 0.1)
                )
                backoff = min(
                    self._persistence_backoff_max_seconds,
                    max(backoff * 2, self._persistence_backoff_initial_seconds),
                )
                continue
            except BaseException:
                await cancel_tasks()
                raise
            else:
                backoff = self._persistence_backoff_initial_seconds

    async def _run_claimed_with_lease(
        self,
        claimed: ClaimedJob,
        gateway: WorkerAsyncGateway,
    ) -> AgentRunResult | TerminalPreflightResult:
        stopped = asyncio.Event()
        cancellation_event = threading.Event()
        heartbeat = asyncio.create_task(self._maintain_lease(claimed, stopped))
        run_task = asyncio.ensure_future(
            self._to_thread(
                self._run_claimed_job,
                claimed,
                gateway,
                cancellation_event,
            )
        )
        deadline = asyncio.create_task(asyncio.sleep(self._job_timeout_seconds))
        try:
            while True:
                tasks: set[asyncio.Future[object]] = {run_task, deadline}
                if heartbeat is not None:
                    tasks.add(heartbeat)
                done, _ = await asyncio.wait(
                    tasks,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if run_task in done:
                    stopped.set()
                    deadline.cancel()
                    lease_owned = True if heartbeat is None else await heartbeat
                    if not lease_owned:
                        raise JobLeaseLostError(
                            "job lease was lost during execution"
                        )
                    result = run_task.result()
                    if result is None:
                        raise RuntimeError("worker execution returned no result")
                    return result
                if heartbeat is not None and heartbeat in done:
                    if not heartbeat.result():
                        stopped.set()
                        cancellation_event.set()
                        deadline.cancel()
                        await self._await_run_shutdown(run_task)
                        raise JobLeaseLostError(
                            "job lease was lost during execution"
                        )
                    # A successful heartbeat task only means that the stop
                    # event was set; keep observing the run task.
                    heartbeat = None
                if deadline in done:
                    stopped.set()
                    cancellation_event.set()
                    await self._await_run_shutdown(run_task)
                    raise RetryableJobError(
                        "job execution timed out",
                        error_code=WorkerErrorCode.WORKER_TIMEOUT.value,
                    )
        except asyncio.CancelledError:
            stopped.set()
            cancellation_event.set()
            await self._await_run_shutdown(run_task)
            raise
        finally:
            stopped.set()
            cancellation_event.set()
            deadline.cancel()
            if heartbeat is not None and not heartbeat.done():
                heartbeat.cancel()
            if not run_task.done():
                run_task.cancel()

    async def _await_run_shutdown(
        self,
        run_task: asyncio.Future[object],
    ) -> None:
        try:
            await asyncio.wait_for(
                asyncio.shield(run_task),
                timeout=self._shutdown_grace_seconds,
            )
        except asyncio.TimeoutError:
            run_task.cancel()
        except BaseException:
            # Lease/timeout classification must not be replaced by a stale
            # thread's provider or tool exception.
            return

    async def _maintain_lease(
        self,
        claimed: ClaimedJob,
        stopped: asyncio.Event,
    ) -> bool:
        interval = max(0.1, self._lease_seconds / 3)
        while True:
            if await self._heartbeat_wait(interval, stopped):
                return True
            try:
                async with self._session_factory() as session:
                    renewed = await JobRepository(session).renew_lease(
                        claimed.id,
                        tenant_id=claimed.tenant_id,
                        attempt_count=claimed.attempt_count,
                        lease_until=self._now()
                        + timedelta(seconds=self._lease_seconds),
                    )
            except Exception:
                self._emit_persistence_failure(
                    trace_id=claimed.trace_id,
                    tenant_id=claimed.tenant_id,
                    job_id=claimed.id,
                    operation=PersistenceOperation.RENEW_LEASE,
                    tenant_config_snapshot=claimed.tenant_config_snapshot,
                    tenant_config_sha256=claimed.tenant_config_sha256,
                )
                return False
            if not renewed:
                return False

    @staticmethod
    async def _default_heartbeat_wait(
        interval: float,
        stopped: asyncio.Event,
    ) -> bool:
        try:
            await asyncio.wait_for(stopped.wait(), timeout=interval)
        except asyncio.TimeoutError:
            return False
        return True

    def _run_claimed_job(
        self,
        claimed: ClaimedJob,
        gateway: WorkerAsyncGateway,
        cancellation_event: threading.Event | None = None,
    ) -> AgentRunResult | TerminalPreflightResult:
        build = self._runtime_factory.build
        parameters = inspect.signature(build).parameters
        if "cancellation_event" in parameters or any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        ):
            runtime = build(
                claimed,
                gateway,
                cancellation_event=cancellation_event,
            )
        else:
            runtime = build(claimed, gateway)
        if isinstance(runtime, TerminalPreflightResult):
            return runtime
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
                raise ProviderRequestRejectedError(
                    "provider rejected request"
                ) from error
            except TimeoutError as error:
                raise RetryableJobError(
                    "worker gateway timed out",
                    error_code=WorkerErrorCode.GATEWAY_TIMEOUT.value,
                ) from error
        finally:
            runtime.close()

    async def _persist_preflight_result(
        self,
        claimed: ClaimedJob,
        result: TerminalPreflightResult,
    ) -> OutcomeCode:
        async with self._session_factory() as session:
            repository = JobRepository(session)
            try:
                if result.reason == "document_unreadable":
                    await repository.mark_succeeded(
                        claimed.id,
                        {
                            "status": "completed",
                            "reason": "executor_stopped",
                            "steps": 0,
                            "execution_path": [],
                            "business_outcome": "routing_only",
                            "routing_status": result.routing_status.value,
                            "routing_reason": result.reason,
                            "missing_required_fields": list(
                                result.missing_required_fields
                            ),
                        },
                        now=self._now(),
                        tenant_id=claimed.tenant_id,
                        attempt_count=claimed.attempt_count,
                    )
                    return OutcomeCode.SUCCEEDED
                await repository.mark_failed(
                    claimed.id,
                    result.reason,
                    now=self._now(),
                    tenant_id=claimed.tenant_id,
                    attempt_count=claimed.attempt_count,
                )
                return OutcomeCode.FAILED
            except JobLeaseLostError:
                raise
            except Exception:
                self._emit_persistence_failure(
                    trace_id=claimed.trace_id,
                    tenant_id=claimed.tenant_id,
                    job_id=claimed.id,
                    operation=(
                        PersistenceOperation.MARK_SUCCEEDED
                        if result.reason == "document_unreadable"
                        else PersistenceOperation.MARK_FAILED
                    ),
                    tenant_config_snapshot=claimed.tenant_config_snapshot,
                    tenant_config_sha256=claimed.tenant_config_sha256,
                )
                raise

    async def _persist_result(
        self,
        claimed: ClaimedJob,
        result: AgentRunResult,
    ) -> OutcomeCode:
        now = self._now()
        if result.status != RunStatus.COMPLETED:
            uncertain = await self._has_side_effect(claimed)
            if uncertain:
                async with self._session_factory() as session:
                    repository = JobRepository(session)
                    try:
                        await repository.mark_failed_uncertain(
                            claimed.id,
                            "agent_failed_after_side_effect",
                            now=now,
                            tenant_id=claimed.tenant_id,
                            attempt_count=claimed.attempt_count,
                        )
                    except JobLeaseLostError:
                        raise
                    except Exception:
                        self._emit_persistence_failure(
                            trace_id=claimed.trace_id,
                            tenant_id=claimed.tenant_id,
                            job_id=claimed.id,
                            operation=PersistenceOperation.MARK_FAILED_UNCERTAIN,
                            tenant_config_snapshot=claimed.tenant_config_snapshot,
                            tenant_config_sha256=claimed.tenant_config_sha256,
                        )
                        raise
                return OutcomeCode.FAILED_UNCERTAIN
        if result.status is RunStatus.COMPLETED and not result.execution_path:
            async with self._session_factory() as session:
                repository = JobRepository(session)
                try:
                    await repository.mark_failed(
                        claimed.id,
                        WorkerErrorCode.AGENT_FAILED.value,
                        now=now,
                        tenant_id=claimed.tenant_id,
                        attempt_count=claimed.attempt_count,
                    )
                except JobLeaseLostError:
                    raise
                except Exception:
                    self._emit_persistence_failure(
                        trace_id=claimed.trace_id,
                        tenant_id=claimed.tenant_id,
                        job_id=claimed.id,
                        operation=PersistenceOperation.MARK_FAILED,
                        tenant_config_snapshot=claimed.tenant_config_snapshot,
                        tenant_config_sha256=claimed.tenant_config_sha256,
                    )
                    raise
            return OutcomeCode.FAILED
        async with self._session_factory() as session:
            repository = JobRepository(session)
            if result.status == RunStatus.COMPLETED:
                summary = self._result_summary(result)
                approval_requested = self._approval_was_requested(result)
                try:
                    if approval_requested:
                        await repository.record_awaiting_approval_result(
                            claimed.id,
                            summary,
                            tenant_id=claimed.tenant_id,
                            attempt_count=claimed.attempt_count,
                        )
                    else:
                        await repository.mark_succeeded(
                            claimed.id,
                            summary,
                            now=now,
                            tenant_id=claimed.tenant_id,
                            attempt_count=claimed.attempt_count,
                        )
                except JobLeaseLostError:
                    raise
                except Exception:
                    self._emit_persistence_failure(
                        trace_id=claimed.trace_id,
                        tenant_id=claimed.tenant_id,
                        job_id=claimed.id,
                        operation=(
                            PersistenceOperation.RECORD_AWAITING_APPROVAL
                            if approval_requested
                            else PersistenceOperation.MARK_SUCCEEDED
                        ),
                        tenant_config_snapshot=claimed.tenant_config_snapshot,
                        tenant_config_sha256=claimed.tenant_config_sha256,
                    )
                    raise
                return (
                    OutcomeCode.NEEDS_APPROVAL
                    if approval_requested
                    else OutcomeCode.SUCCEEDED
                )
            else:
                try:
                    error_code = (
                        WorkerErrorCode.TOOL_EXECUTION_FAILED.value
                        if result.reason is StopReason.TOOL_EXECUTION_FAILED
                        else WorkerErrorCode.AGENT_FAILED.value
                    )
                    await repository.mark_failed(
                        claimed.id,
                        error_code,
                        now=now,
                        tenant_id=claimed.tenant_id,
                        attempt_count=claimed.attempt_count,
                    )
                except JobLeaseLostError:
                    raise
                except Exception:
                    self._emit_persistence_failure(
                        trace_id=claimed.trace_id,
                        tenant_id=claimed.tenant_id,
                        job_id=claimed.id,
                        operation=PersistenceOperation.MARK_FAILED,
                        tenant_config_snapshot=claimed.tenant_config_snapshot,
                        tenant_config_sha256=claimed.tenant_config_sha256,
                    )
                    raise
                return OutcomeCode.FAILED

    async def _schedule_retry(
        self,
        claimed: ClaimedJob,
        error_code: str,
    ) -> float:
        now = self._now()
        delay = self._retry_delay_with_jitter(claimed.attempt_count)
        async with self._session_factory() as session:
            repository = JobRepository(session)
            try:
                await repository.schedule_retry(
                    claimed.id,
                    error_code,
                    available_at=now + timedelta(seconds=delay),
                    tenant_id=claimed.tenant_id,
                    attempt_count=claimed.attempt_count,
                )
            except JobLeaseLostError:
                raise
            except Exception:
                self._emit_persistence_failure(
                    trace_id=claimed.trace_id,
                    tenant_id=claimed.tenant_id,
                    job_id=claimed.id,
                    operation=PersistenceOperation.SCHEDULE_RETRY,
                    tenant_config_snapshot=claimed.tenant_config_snapshot,
                    tenant_config_sha256=claimed.tenant_config_sha256,
                )
                raise
        return delay

    async def _mark_failed(
        self,
        claimed: ClaimedJob,
        error_code: str,
    ) -> None:
        async with self._session_factory() as session:
            repository = JobRepository(session)
            try:
                await repository.mark_failed(
                    claimed.id,
                    error_code,
                    now=self._now(),
                    tenant_id=claimed.tenant_id,
                    attempt_count=claimed.attempt_count,
                )
            except JobLeaseLostError:
                raise
            except Exception:
                self._emit_persistence_failure(
                    trace_id=claimed.trace_id,
                    tenant_id=claimed.tenant_id,
                    job_id=claimed.id,
                    operation=PersistenceOperation.MARK_FAILED,
                    tenant_config_snapshot=claimed.tenant_config_snapshot,
                    tenant_config_sha256=claimed.tenant_config_sha256,
                )
                raise

    async def _mark_failed_uncertain(
        self,
        claimed: ClaimedJob,
        error_code: str,
    ) -> None:
        async with self._session_factory() as session:
            repository = JobRepository(session)
            try:
                await repository.mark_failed_uncertain(
                    claimed.id,
                    error_code,
                    now=self._now(),
                    tenant_id=claimed.tenant_id,
                    attempt_count=claimed.attempt_count,
                )
            except JobLeaseLostError:
                raise
            except Exception:
                self._emit_persistence_failure(
                    trace_id=claimed.trace_id,
                    tenant_id=claimed.tenant_id,
                    job_id=claimed.id,
                    operation=PersistenceOperation.MARK_FAILED_UNCERTAIN,
                    tenant_config_snapshot=claimed.tenant_config_snapshot,
                    tenant_config_sha256=claimed.tenant_config_sha256,
                )
                raise

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
                attempt_count=claimed.attempt_count,
            )
            return marker is not None

    def _emit_job_claimed(self, claimed: ClaimedJob) -> None:
        self._emit_worker_event(
            event=EventName.WORKER_JOB_CLAIMED,
            claimed=claimed,
            outcome=OutcomeCode.CLAIMED,
            attempt_count=claimed.attempt_count,
        )

    def _emit_job_started(self, claimed: ClaimedJob) -> None:
        self._emit_worker_event(
            event=EventName.WORKER_JOB_RUN_STARTED,
            claimed=claimed,
            outcome=OutcomeCode.STARTED,
            attempt_count=claimed.attempt_count,
        )

    def _emit_retry_scheduled(
        self,
        claimed: ClaimedJob,
        *,
        error_code: str,
        retry_delay_ms: int,
    ) -> None:
        self._emit_worker_event(
            event=EventName.WORKER_JOB_RETRY_SCHEDULED,
            claimed=claimed,
            outcome=OutcomeCode.RETRY_SCHEDULED,
            attempt_count=claimed.attempt_count,
            retry_delay_ms=retry_delay_ms,
            worker_error_code=self._worker_error_code(error_code),
        )

    def _emit_lease_recovered(self, observation: LeaseRecoveryObservation) -> None:
        context = self._context_for_observation(
            trace_id=observation.trace_id,
            tenant_id=observation.tenant_id,
            job_id=observation.job_id,
            tenant_config_snapshot=observation.tenant_config_snapshot,
            tenant_config_sha256=observation.tenant_config_sha256,
        )
        self._emit_observation(
            ObservationEvent(
                event=EventName.WORKER_JOB_LEASE_RECOVERED,
                level=EventLevel.WARNING,
                trace_id=context.trace_id,
                tenant_id=context.tenant_id,
                job_id=context.job_id,
                component=Component.WORKER,
                outcome=OutcomeCode.RECOVERED,
                attempt_count=observation.attempt_count,
                worker_error_code=self._worker_error_code(observation.error_code),
                scenario_key=context.scenario_key,
                profile_fingerprint=context.profile_fingerprint,
            )
        )

    def _emit_expired_approval(
        self, observation: ExpiredApprovalObservation
    ) -> None:
        duration_ms = max(
            0,
            int(
                (
                    observation.decided_at - observation.created_at
                ).total_seconds()
                * 1000
            ),
        )
        context = self._context_for_observation(
            trace_id=observation.trace_id,
            tenant_id=observation.tenant_id,
            job_id=observation.job_id,
            tenant_config_snapshot=observation.tenant_config_snapshot,
            tenant_config_sha256=observation.tenant_config_sha256,
        )
        self._emit_observation(
            ObservationEvent(
                event=EventName.APPROVAL_EXPIRED,
                level=EventLevel.INFO,
                trace_id=context.trace_id,
                tenant_id=context.tenant_id,
                job_id=context.job_id,
                approval_id=observation.approval_id,
                component=Component.APPROVAL,
                outcome=OutcomeCode.EXPIRED,
                duration_ms=duration_ms,
                scenario_key=context.scenario_key,
                profile_fingerprint=context.profile_fingerprint,
            )
        )

    def _emit_job_finished(
        self,
        claimed: ClaimedJob,
        *,
        outcome: OutcomeCode,
        duration_ms: int,
        worker_error_code: WorkerErrorCode | None = None,
        steps: int | None = None,
        stop_reason: StopReason | None = None,
        side_effect_committed: bool | None = None,
    ) -> None:
        self._emit_worker_event(
            event=EventName.WORKER_JOB_FINISHED,
            claimed=claimed,
            outcome=outcome,
            attempt_count=claimed.attempt_count,
            duration_ms=duration_ms,
            queue_latency_ms=self._queue_latency_ms(claimed),
            total_latency_ms=self._total_latency_ms(claimed),
            worker_error_code=worker_error_code,
            steps=steps,
            stop_reason=stop_reason,
            side_effect_committed=side_effect_committed,
        )

    def _emit_worker_event(
        self,
        *,
        event: EventName,
        claimed: ClaimedJob,
        outcome: OutcomeCode,
        attempt_count: int | None = None,
        duration_ms: int | None = None,
        queue_latency_ms: int | None = None,
        total_latency_ms: int | None = None,
        retry_delay_ms: int | None = None,
        worker_error_code: WorkerErrorCode | None = None,
        steps: int | None = None,
        stop_reason: StopReason | None = None,
        side_effect_committed: bool | None = None,
    ) -> None:
        context = self._context_for_claimed(claimed)
        self._emit_observation(
            ObservationEvent(
                event=event,
                level=(
                    EventLevel.ERROR
                    if outcome in {
                        OutcomeCode.FAILED,
                        OutcomeCode.FAILED_UNCERTAIN,
                    }
                    else EventLevel.INFO
                ),
                trace_id=context.trace_id,
                tenant_id=context.tenant_id,
                job_id=context.job_id,
                component=Component.WORKER,
                outcome=outcome,
                duration_ms=duration_ms,
                queue_latency_ms=queue_latency_ms,
                total_latency_ms=total_latency_ms,
                attempt_count=attempt_count,
                retry_delay_ms=retry_delay_ms,
                worker_error_code=worker_error_code,
                steps=steps,
                stop_reason=stop_reason,
                side_effect_committed=side_effect_committed,
                scenario_key=context.scenario_key,
                profile_fingerprint=context.profile_fingerprint,
            )
        )

    def _context_for_claimed(self, claimed: ClaimedJob) -> ObservationContext:
        return self._context_for_observation(
            trace_id=claimed.trace_id,
            tenant_id=claimed.tenant_id,
            job_id=claimed.id,
            tenant_config_snapshot=claimed.tenant_config_snapshot,
            tenant_config_sha256=claimed.tenant_config_sha256,
        )

    @staticmethod
    def _context_for_observation(
        *,
        trace_id: UUID,
        tenant_id: UUID,
        job_id: UUID,
        tenant_config_snapshot: Mapping[str, object],
        tenant_config_sha256: str | None,
    ) -> ObservationContext:
        context = ObservationContext(
            trace_id=trace_id,
            tenant_id=tenant_id,
            job_id=job_id,
        )
        if not tenant_config_snapshot or tenant_config_sha256 is None:
            return context
        try:
            profile = resolve_persisted_profile(
                tenant_config_snapshot,
                tenant_config_sha256,
            )
        except Exception:
            return context
        if profile.compiled is None:
            return context
        return context.bind(
            scenario_key=profile.compiled.scenario_key,
            profile_fingerprint=profile.compiled.profile_fingerprint,
        )

    def _emit_persistence_failure(
        self,
        *,
        trace_id: UUID | None,
        tenant_id: UUID | None,
        job_id: UUID | None,
        operation: PersistenceOperation,
        tenant_config_snapshot: Mapping[str, object] | None = None,
        tenant_config_sha256: str | None = None,
    ) -> None:
        context = ObservationContext(
            trace_id=trace_id or uuid4(),
            tenant_id=tenant_id,
            job_id=job_id,
        )
        if (
            tenant_id is not None
            and job_id is not None
            and tenant_config_snapshot is not None
            and tenant_config_sha256 is not None
        ):
            context = self._context_for_observation(
                trace_id=context.trace_id,
                tenant_id=tenant_id,
                job_id=job_id,
                tenant_config_snapshot=tenant_config_snapshot,
                tenant_config_sha256=tenant_config_sha256,
            )
        self._emit_observation(
            ObservationEvent(
                event=EventName.PERSISTENCE_OPERATION_FAILED,
                level=EventLevel.ERROR,
                trace_id=context.trace_id,
                tenant_id=context.tenant_id,
                job_id=context.job_id,
                component=Component.PERSISTENCE,
                outcome=OutcomeCode.FAILED,
                persistence_operation=operation,
                scenario_key=context.scenario_key,
                profile_fingerprint=context.profile_fingerprint,
            )
        )

    def _emit_observation(self, event: ObservationEvent) -> None:
        safe_emit(self._observer, event)

    @staticmethod
    def _worker_error_code(value: str) -> WorkerErrorCode | None:
        try:
            return WorkerErrorCode(value)
        except ValueError:
            return None

    @staticmethod
    def _queue_latency_ms(claimed: ClaimedJob) -> int:
        return max(
            0,
            int((claimed.started_at - claimed.created_at).total_seconds() * 1000),
        )

    def _total_latency_ms(self, claimed: ClaimedJob) -> int:
        return max(
            0,
            int((self._now() - claimed.created_at).total_seconds() * 1000),
        )

    def _elapsed_ms(self, started_ns: int) -> int:
        return max(0, (self._monotonic_clock() - started_ns) // 1_000_000)

    @staticmethod
    def _approval_was_requested(result: AgentRunResult) -> bool:
        return result.final_response == "approval_requested" and any(
            item.action_known and item.policy_decision == "needs_approval"
            for item in result.execution_path
        )

    @staticmethod
    def _result_summary(result: AgentRunResult) -> dict[str, object]:
        execution_path = [
            {
                "step": item.step,
                "action_key": item.action_key,
                "action_known": item.action_known,
                "policy_decision": item.policy_decision,
                "routing_status": item.routing_status,
                "policy_reason": item.policy_reason,
                "missing_required_fields": list(item.missing_required_fields),
                "tool_outcome": item.tool_outcome,
                "side_effect_committed": item.side_effect_committed,
            }
            for item in result.execution_path
        ]
        summary: dict[str, object] = {
            "status": result.status.value,
            "reason": result.reason.value,
            "steps": result.steps,
            "execution_path": execution_path,
        }
        if execution_path:
            # A closing no-tool proposal is policy-evaluated for safety, but it
            # must not overwrite the business outcome of an earlier executed
            # action (for example, an empty follow-up after create_case).
            terminal = next(
                (
                    item
                    for item in reversed(execution_path)
                    if item["action_key"] is not None
                ),
                execution_path[-1],
            )
            summary["routing_status"] = terminal["routing_status"]
            summary["routing_reason"] = terminal["policy_reason"]
            summary["missing_required_fields"] = terminal[
                "missing_required_fields"
            ]
            summary["business_outcome"] = (
                "tool_result"
                if terminal["tool_outcome"] is not None
                else (
                    "approval_requested"
                    if AgentWorker._approval_was_requested(result)
                    else "routing_only"
                )
            )
        return summary

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

    @property
    def _persistence_backoff_initial_seconds(self) -> float:
        return float(self._settings.worker_persistence_backoff_initial_seconds)

    @property
    def _persistence_backoff_max_seconds(self) -> float:
        return float(self._settings.worker_persistence_backoff_max_seconds)

    @property
    def _job_timeout_seconds(self) -> float:
        return float(self._settings.worker_job_timeout_seconds)

    @property
    def _gateway_timeout_seconds(self) -> float:
        return float(self._settings.worker_gateway_timeout_seconds)

    @property
    def _worker_concurrency(self) -> int:
        return int(self._settings.worker_concurrency)

    @property
    def _shutdown_grace_seconds(self) -> float:
        return float(self._settings.worker_shutdown_grace_seconds)


__all__ = [
    "AgentWorker",
    "RetryableJobError",
    "TransientPersistenceError",
    "WorkerRetryableError",
]
