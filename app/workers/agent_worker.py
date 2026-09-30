"""Run claimed agent jobs outside the FastAPI request lifecycle.

The worker owns the async event loop and every SQLAlchemy session.  A claimed
job is handed to a synchronous thread only after its lease transaction has
committed; synchronous tool calls use ``WorkerAsyncGateway`` to schedule
coroutines back on this owner loop.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import httpx

from app.agent.models import AgentRunResult, RunStatus, StopReason
from app.core.config import Settings, settings
from app.domain.approval_repository import ApprovalRepository, ExpiredApprovalObservation
from app.domain.job_repository import (
    ClaimedJob,
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

    async def serve_once(self) -> bool:
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

        self._emit_job_claimed(claimed)
        self._emit_job_started(claimed)
        gateway = WorkerAsyncGateway(asyncio.get_running_loop())
        run_started_ns = self._monotonic_clock()
        finished = False
        try:
            try:
                result = await self._to_thread(
                    self._run_claimed_job,
                    claimed,
                    gateway,
                )
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
        """Run until cancelled, polling only when no job was available."""

        while True:
            claimed = await self.serve_once()
            if not claimed:
                await self._sleep(self._poll_interval_seconds)

    def _run_claimed_job(
        self,
        claimed: ClaimedJob,
        gateway: WorkerAsyncGateway,
    ) -> AgentRunResult | TerminalPreflightResult:
        runtime = self._runtime_factory.build(claimed, gateway)
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
                raise
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
                            "final_response": "document_unreadable",
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
        async with self._session_factory() as session:
            repository = JobRepository(session)
            if result.status == RunStatus.COMPLETED:
                summary = self._result_summary(result)
                if "document" in claimed.source_snapshot:
                    # Provider rationale can repeat source text. Keep the
                    # durable document outcome to the server-owned routing
                    # summary and retain the legacy key with a null value.
                    summary["final_response"] = None
                try:
                    await repository.mark_succeeded(
                        claimed.id,
                        summary,
                        now=now,
                        tenant_id=claimed.tenant_id,
                        attempt_count=claimed.attempt_count,
                    )
                except Exception:
                    self._emit_persistence_failure(
                        trace_id=claimed.trace_id,
                        tenant_id=claimed.tenant_id,
                        job_id=claimed.id,
                        operation=PersistenceOperation.MARK_SUCCEEDED,
                        tenant_config_snapshot=claimed.tenant_config_snapshot,
                        tenant_config_sha256=claimed.tenant_config_sha256,
                    )
                    raise
                return OutcomeCode.SUCCEEDED
            else:
                try:
                    await repository.mark_failed(
                        claimed.id,
                        "agent_failed",
                        now=now,
                        tenant_id=claimed.tenant_id,
                        attempt_count=claimed.attempt_count,
                    )
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
    def _result_summary(result: AgentRunResult) -> dict[str, object]:
        summary: dict[str, object] = {
            "status": result.status.value,
            "reason": result.reason.value,
            "steps": result.steps,
            "final_response": result.final_response,
        }
        for message in reversed(result.messages):
            if message.role.value != "tool":
                continue
            data = message.tool_result
            if data is None or not isinstance(data, Mapping):
                break
            status = data.get("status")
            reason = data.get("reason")
            missing = data.get("missing_required_fields")
            if (
                isinstance(status, str)
                and isinstance(reason, str)
                and isinstance(missing, list)
                and all(isinstance(item, str) for item in missing)
            ):
                summary["routing_status"] = status
                summary["routing_reason"] = reason
                summary["missing_required_fields"] = sorted(missing)
            break
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


__all__ = [
    "AgentWorker",
    "RetryableJobError",
    "WorkerRetryableError",
]
