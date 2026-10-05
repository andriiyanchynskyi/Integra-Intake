"""Offline orchestration tests for the isolated agent worker."""

from __future__ import annotations

import asyncio
import hashlib
import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
import httpx
import yaml

from app.agent.models import AgentMessage, AgentRunResult, MessageRole, RunStatus, StopReason
from app.core.config import Settings
from app.documents import DocumentExtractionError
from app.domain.approval_repository import ExpiredApprovalObservation
from app.domain.job_repository import ClaimedJob, JobLeaseLostError, LeaseRecoveryObservation
from app.observability import EventName, OutcomeCode, RecordingObserver, WorkerErrorCode
from app.runtime.preflight import TerminalPreflightResult
from app.runtime.profiles import canonical_json_bytes
from app.tenants.config import RoutingStatus
from app.workers.agent_worker import AgentWorker, RetryableJobError


_REPAIR_PROFILE_SNAPSHOT = yaml.safe_load(
    (Path(__file__).parents[1] / "examples" / "repair-service.yaml").read_text(
        encoding="utf-8"
    )
)
_REPAIR_PROFILE_SHA256 = hashlib.sha256(
    canonical_json_bytes(_REPAIR_PROFILE_SNAPSHOT)
).hexdigest()


def _claimed(*, side_effect_committed_at: datetime | None = None) -> ClaimedJob:
    now = datetime.now(timezone.utc)
    return ClaimedJob(
        id=uuid4(),
        tenant_id=uuid4(),
        trace_id=uuid4(),
        status="running",
        source_snapshot={"channel": "email", "subject": "Load", "body": "Need a truck"},
        tenant_config_snapshot={"display_name": "test"},
        tenant_config_sha256="a" * 64,
        risk_signals={"safety_or_legal_risk": False},
        attempt_count=1,
        side_effect_committed_at=side_effect_committed_at,
        created_at=now - timedelta(seconds=1),
        available_at=now - timedelta(seconds=1),
        started_at=now,
    )


def _repair_claimed(
    *, side_effect_committed_at: datetime | None = None
) -> ClaimedJob:
    return replace(
        _claimed(side_effect_committed_at=side_effect_committed_at),
        tenant_config_snapshot=_REPAIR_PROFILE_SNAPSHOT,
        tenant_config_sha256=_REPAIR_PROFILE_SHA256,
    )


def _http_status_error(status_code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://provider.example/v1/chat/completions")
    response = httpx.Response(
        status_code,
        request=request,
        text="provider-body-secret",
    )
    return httpx.HTTPStatusError(
        "provider request failed",
        request=request,
        response=response,
    )


class _Session:
    async def __aenter__(self) -> "_Session":
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    async def close(self) -> None:
        return None


class _SessionFactory:
    def __init__(self, session: _Session | None = None) -> None:
        self.session = session or _Session()

    def __call__(self) -> _Session:
        return self.session


_WORKER_CALLS: list[str] = []


class _FakeApprovalRepository:
    instances: list["_FakeApprovalRepository"] = []
    expired: tuple[ExpiredApprovalObservation, ...] = ()

    def __init__(self, session: object) -> None:
        self.calls: list[tuple[str, object]] = []
        _FakeApprovalRepository.instances.append(self)

    async def expire_due(self, **kwargs: object) -> object:
        self.calls.append(("expire_due", kwargs))
        _WORKER_CALLS.append("expire_due")
        return type(self).expired


class _FakeRepository:
    instances: list["_FakeRepository"] = []
    next_claimed: ClaimedJob | None = None
    recovered: tuple[LeaseRecoveryObservation, ...] = ()
    lease_owned = True
    lease_lost_on_mark_succeeded = False

    def __init__(self, session: object) -> None:
        self.claimed = _FakeRepository.next_claimed
        _FakeRepository.next_claimed = None
        self.calls: list[tuple[str, object]] = []
        self.now: datetime | None = None
        _FakeRepository.instances.append(self)

    async def recover_expired_leases(self, **kwargs: object) -> object:
        self.calls.append(("recover_expired_leases", kwargs))
        _WORKER_CALLS.append("recover_expired_leases")
        self.now = kwargs["now"]  # type: ignore[assignment]
        return type(self).recovered

    async def claim_next(self, **kwargs: object) -> ClaimedJob | None:
        self.calls.append(("claim_next", kwargs))
        _WORKER_CALLS.append("claim_next")
        result, self.claimed = self.claimed, None
        return result

    async def renew_lease(self, *args: object, **kwargs: object) -> bool:
        self.calls.append(("renew_lease", (args, kwargs)))
        return type(self).lease_owned

    async def mark_succeeded(self, *args: object, **kwargs: object) -> None:
        if type(self).lease_lost_on_mark_succeeded:
            raise JobLeaseLostError("stale worker")
        if self.claimed is not None and self.claimed.status != "running":
            return
        self.calls.append(("mark_succeeded", (args, kwargs)))

    async def mark_failed(self, *args: object, **kwargs: object) -> None:
        if self.claimed is not None and self.claimed.status != "running":
            return
        self.calls.append(("mark_failed", (args, kwargs)))

    async def schedule_retry(self, *args: object, **kwargs: object) -> None:
        self.calls.append(("schedule_retry", (args, kwargs)))

    async def mark_failed_uncertain(self, *args: object, **kwargs: object) -> None:
        if self.claimed is not None and self.claimed.status != "running":
            return
        self.calls.append(("mark_failed_uncertain", (args, kwargs)))


class _Loop:
    def __init__(self, result: AgentRunResult | BaseException) -> None:
        self.result = result
        self.thread_id: int | None = None

    def run(self, messages: object) -> AgentRunResult:
        self.thread_id = threading.get_ident()
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


class _Runtime:
    def __init__(self, result: AgentRunResult | BaseException) -> None:
        self.loop = _Loop(result)
        self.initial_messages: tuple[object, ...] = ()
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _RuntimeFactory:
    def __init__(self, runtime: object) -> None:
        self.runtime = runtime
        self.calls: list[tuple[ClaimedJob, object]] = []

    def build(self, claimed: ClaimedJob, gateway: object) -> object:
        self.calls.append((claimed, gateway))
        return self.runtime


def _result(*, status: RunStatus, reason: StopReason, final_response: str | None) -> AgentRunResult:
    return AgentRunResult(
        status=status,
        reason=reason,
        messages=(),
        steps=2,
        final_response=final_response,
    )


def _terminal_preflight(
    *, reason: str, routing_status: RoutingStatus
) -> TerminalPreflightResult:
    return TerminalPreflightResult(
        routing_status=routing_status,
        reason=reason,  # type: ignore[arg-type]
        target_intake_type="synthetic_intake",
        missing_required_fields=("required_field",),
        document_kind="synthetic_document",
        extraction_error=(
            DocumentExtractionError.PDF_MALFORMED
            if reason == "document_unreadable"
            else None
        ),
    )


def test_result_summary_keeps_only_safe_routing_fields_from_terminal_tool_data() -> None:
    result = AgentRunResult(
        status=RunStatus.COMPLETED,
        reason=StopReason.EXECUTOR_STOPPED,
        messages=(
            AgentMessage(
                role=MessageRole.TOOL,
                tool_result={
                    "status": "awaiting_input",
                    "reason": "document_unreadable",
                    "missing_required_fields": ["valid_until", "origin"],
                    "source": "must not persist",
                    "document": "must not persist",
                    "proposal": {"confidence": 0.99},
                    "source_excerpt": "must not persist",
                },
            ),
        ),
        steps=2,
        final_response="document_unreadable",
    )

    summary = AgentWorker._result_summary(result)

    assert set(summary) == {
        "status",
        "reason",
        "steps",
        "routing_status",
        "routing_reason",
        "missing_required_fields",
    }
    assert summary == {
        "status": "completed",
        "reason": "executor_stopped",
        "steps": 2,
        "routing_status": "awaiting_input",
        "routing_reason": "document_unreadable",
        "missing_required_fields": ["origin", "valid_until"],
    }


def test_result_summary_does_not_reuse_routing_data_before_none_tool_result() -> None:
    result = AgentRunResult(
        status=RunStatus.COMPLETED,
        reason=StopReason.EXECUTOR_STOPPED,
        messages=(
            AgentMessage(
                role=MessageRole.TOOL,
                tool_result={
                    "status": "awaiting_input",
                    "reason": "missing_required_fields",
                    "missing_required_fields": ["origin"],
                },
            ),
            AgentMessage(role=MessageRole.TOOL, tool_result=None),
        ),
        steps=2,
        final_response="tool_result_unavailable",
    )

    summary = AgentWorker._result_summary(result)

    assert summary == {
        "status": "completed",
        "reason": "executor_stopped",
        "steps": 2,
    }


@pytest.mark.asyncio
async def test_persist_result_redacts_document_response_from_job_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_repository(monkeypatch)
    claimed = replace(
        _claimed(),
        source_snapshot={
            "channel": "email",
            "subject": "Rate confirmation",
            "body": "Rate confirmation document received.",
            "document": {
                "kind": "rate_confirmation",
                "media_type": "text/plain",
                "sha256": "a" * 64,
                "parser_version": "rate_confirmation_document.v1",
                "text": "secret document excerpt",
            },
        },
    )
    _FakeRepository.next_claimed = claimed
    worker = AgentWorker(_SessionFactory(), settings=_settings())

    await worker._persist_result(
        claimed,
        _result(
            status=RunStatus.COMPLETED,
            reason=StopReason.EXECUTOR_STOPPED,
            final_response="secret document excerpt",
        ),
    )

    repository = _FakeRepository.instances[-1]
    succeeded = [entry for entry in repository.calls if entry[0] == "mark_succeeded"]
    assert succeeded
    summary = succeeded[-1][1][0][1]  # type: ignore[index]
    assert summary == {
        "status": "completed",
        "reason": "executor_stopped",
        "steps": 2,
    }
    assert "secret document excerpt" not in repr(summary)


def _settings() -> Settings:
    return Settings(
        worker_poll_interval_seconds=0.001,
        worker_lease_seconds=60,
        worker_max_retries=4,
    )


def _install_repository(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeRepository.instances.clear()
    _FakeRepository.next_claimed = None
    _FakeRepository.recovered = ()
    _FakeRepository.lease_owned = True
    _FakeRepository.lease_lost_on_mark_succeeded = False
    _FakeApprovalRepository.instances.clear()
    _FakeApprovalRepository.expired = ()
    _WORKER_CALLS.clear()
    monkeypatch.setattr("app.workers.agent_worker.JobRepository", _FakeRepository)
    monkeypatch.setattr(
        "app.workers.agent_worker.ApprovalRepository", _FakeApprovalRepository
    )


@pytest.mark.asyncio
async def test_serve_once_runs_loop_through_injected_to_thread_seam(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_repository(monkeypatch)
    claimed = _claimed()
    _FakeRepository.next_claimed = claimed
    runtime = _Runtime(
        _result(
            status=RunStatus.COMPLETED,
            reason=StopReason.FINAL,
            final_response="done",
        )
    )
    runtime_factory = _RuntimeFactory(runtime)
    thread_calls: list[tuple[object, ...]] = []
    event_loop_thread = threading.get_ident()

    async def injected_to_thread(function: object, *args: object) -> object:
        thread_calls.append((function, *args))
        return await asyncio.to_thread(function, *args)

    worker = AgentWorker(
        _SessionFactory(),
        runtime_factory=runtime_factory,
        settings=_settings(),
        to_thread=injected_to_thread,
    )
    result = await worker.serve_once()

    assert result is True
    assert thread_calls
    assert runtime.loop.thread_id is not None
    assert runtime.loop.thread_id != event_loop_thread
    assert runtime.closed is True
    assert runtime_factory.calls and runtime_factory.calls[0][0] == claimed
    repository = _FakeRepository.instances[-1]
    succeeded = [entry for entry in repository.calls if entry[0] == "mark_succeeded"]
    assert succeeded
    summary = succeeded[-1][1][0][1]  # type: ignore[index]
    assert set(summary) == {"status", "reason", "steps"}
    assert summary == {
        "status": "completed",
        "reason": "final",
        "steps": 2,
    }


@pytest.mark.asyncio
async def test_lease_loss_skips_terminal_persistence_and_emits_safe_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_repository(monkeypatch)
    claimed = _claimed()
    _FakeRepository.next_claimed = claimed
    _FakeRepository.lease_owned = False
    runtime = _Runtime(
        _result(
            status=RunStatus.COMPLETED,
            reason=StopReason.FINAL,
            final_response="done",
        )
    )

    async def heartbeat_wait(_interval: float, _stopped: asyncio.Event) -> bool:
        return False

    async def injected_to_thread(function: object, *args: object) -> object:
        await asyncio.sleep(0)
        return await asyncio.to_thread(function, *args)

    observer = RecordingObserver()
    worker = AgentWorker(
        _SessionFactory(),
        runtime_factory=_RuntimeFactory(runtime),
        settings=_settings(),
        observer=observer,
        to_thread=injected_to_thread,
        heartbeat_wait=heartbeat_wait,
    )

    assert await worker.serve_once() is True
    all_calls = [entry for instance in _FakeRepository.instances for entry in instance.calls]
    assert [entry for entry in all_calls if entry[0] == "renew_lease"]
    assert not [entry for entry in all_calls if entry[0] == "mark_succeeded"]
    finished = [
        event for event in observer.events if event.event is EventName.WORKER_JOB_FINISHED
    ]
    assert finished[-1].outcome is OutcomeCode.LEASE_LOST
    assert finished[-1].worker_error_code is WorkerErrorCode.LEASE_LOST


@pytest.mark.asyncio
async def test_stale_terminal_persistence_is_not_reported_as_generic_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_repository(monkeypatch)
    claimed = _claimed()
    _FakeRepository.next_claimed = claimed
    _FakeRepository.lease_lost_on_mark_succeeded = True
    runtime = _Runtime(
        _result(
            status=RunStatus.COMPLETED,
            reason=StopReason.FINAL,
            final_response="done",
        )
    )
    observer = RecordingObserver()
    worker = AgentWorker(
        _SessionFactory(),
        runtime_factory=_RuntimeFactory(runtime),
        settings=_settings(),
        observer=observer,
        to_thread=asyncio.to_thread,
    )

    assert await worker.serve_once() is True
    finished = [
        event for event in observer.events if event.event is EventName.WORKER_JOB_FINISHED
    ]
    assert finished[-1].outcome is OutcomeCode.LEASE_LOST
    assert finished[-1].worker_error_code is WorkerErrorCode.LEASE_LOST
    assert not [
        event
        for event in observer.events
        if event.event is EventName.PERSISTENCE_OPERATION_FAILED
    ]


@pytest.mark.asyncio
async def test_terminal_unreadable_preflight_is_persisted_as_safe_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_repository(monkeypatch)
    claimed = _claimed()
    _FakeRepository.next_claimed = claimed
    terminal = _terminal_preflight(
        reason="document_unreadable",
        routing_status=RoutingStatus.AWAITING_INPUT,
    )
    runtime_factory = _RuntimeFactory(terminal)
    observer = RecordingObserver()
    worker = AgentWorker(
        _SessionFactory(),
        runtime_factory=runtime_factory,
        settings=_settings(),
        observer=observer,
        to_thread=asyncio.to_thread,
    )

    assert await worker.serve_once() is True
    assert runtime_factory.calls and runtime_factory.calls[0][0] == claimed
    assert not hasattr(terminal, "loop")

    repository = _FakeRepository.instances[-1]
    succeeded = [entry for entry in repository.calls if entry[0] == "mark_succeeded"]
    assert succeeded
    summary = succeeded[-1][1][0][1]  # type: ignore[index]
    assert summary == {
        "status": "completed",
        "reason": "executor_stopped",
        "steps": 0,
        "routing_status": "awaiting_input",
        "routing_reason": "document_unreadable",
        "missing_required_fields": ["required_field"],
    }
    assert "synthetic_document" not in repr(summary)
    assert not [entry for entry in repository.calls if entry[0] == "mark_failed"]
    assert not [entry for entry in repository.calls if entry[0] == "schedule_retry"]
    finished = [
        event for event in observer.events if event.event is EventName.WORKER_JOB_FINISHED
    ]
    assert finished[-1].outcome is OutcomeCode.SUCCEEDED
    assert finished[-1].worker_error_code is None


@pytest.mark.asyncio
async def test_capability_unavailable_preflight_is_non_retryable_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_repository(monkeypatch)
    claimed = _repair_claimed()
    _FakeRepository.next_claimed = claimed
    terminal = _terminal_preflight(
        reason="capability_unavailable",
        routing_status=RoutingStatus.REJECTED,
    )
    runtime_factory = _RuntimeFactory(terminal)
    observer = RecordingObserver()
    worker = AgentWorker(
        _SessionFactory(),
        runtime_factory=runtime_factory,
        settings=_settings(),
        observer=observer,
        to_thread=asyncio.to_thread,
    )

    assert await worker.serve_once() is True
    assert not hasattr(terminal, "loop")

    repository = _FakeRepository.instances[-1]
    failed = [entry for entry in repository.calls if entry[0] == "mark_failed"]
    assert failed
    assert failed[-1][1][0][1] == "capability_unavailable"  # type: ignore[index]
    assert not [entry for entry in repository.calls if entry[0] == "mark_succeeded"]
    assert not [entry for entry in repository.calls if entry[0] == "schedule_retry"]
    finished = [
        event for event in observer.events if event.event is EventName.WORKER_JOB_FINISHED
    ]
    assert finished[-1].outcome is OutcomeCode.FAILED
    assert finished[-1].worker_error_code is WorkerErrorCode.CAPABILITY_UNAVAILABLE


@pytest.mark.asyncio
async def test_worker_emits_one_trace_bound_lifecycle_for_successful_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_repository(monkeypatch)
    claimed = _repair_claimed()
    _FakeRepository.next_claimed = claimed
    observer = RecordingObserver()
    ticks = iter((1_000_000, 7_000_000))
    runtime = _Runtime(
        _result(
            status=RunStatus.COMPLETED,
            reason=StopReason.FINAL,
            final_response="done",
        )
    )
    worker = AgentWorker(
        _SessionFactory(),
        runtime_factory=_RuntimeFactory(runtime),
        settings=_settings(),
        observer=observer,
        monotonic_clock=lambda: next(ticks),
        to_thread=asyncio.to_thread,
    )

    assert await worker.serve_once() is True
    events = [
        event
        for event in observer.events
        if event.event
        in {
            EventName.WORKER_JOB_CLAIMED,
            EventName.WORKER_JOB_RUN_STARTED,
            EventName.WORKER_JOB_FINISHED,
        }
    ]
    assert [event.event for event in events] == [
        EventName.WORKER_JOB_CLAIMED,
        EventName.WORKER_JOB_RUN_STARTED,
        EventName.WORKER_JOB_FINISHED,
    ]
    assert all(event.trace_id == claimed.trace_id for event in events)
    assert all(event.scenario_key == "repair_service" for event in events)
    assert all(event.profile_fingerprint == _REPAIR_PROFILE_SHA256 for event in events)
    assert events[-1].outcome is OutcomeCode.SUCCEEDED
    assert events[-1].steps == 2
    assert events[-1].stop_reason is StopReason.FINAL
    assert events[-1].duration_ms == 6
    assert events[-1].queue_latency_ms is not None
    assert events[-1].total_latency_ms is not None
    assert events[-1].queue_latency_ms >= 0
    assert events[-1].total_latency_ms >= 0


@pytest.mark.asyncio
async def test_worker_emits_expiry_and_lease_recovery_without_source_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_repository(monkeypatch)
    trace_id = uuid4()
    tenant_id = uuid4()
    job_id = uuid4()
    approval_id = uuid4()
    created_at = datetime(2026, 9, 23, 12, tzinfo=timezone.utc)
    _FakeApprovalRepository.expired = (
        ExpiredApprovalObservation(
            trace_id=trace_id,
            tenant_id=tenant_id,
            job_id=job_id,
            approval_id=approval_id,
            created_at=created_at,
            decided_at=created_at + timedelta(seconds=4),
        ),
    )
    _FakeRepository.recovered = (
        LeaseRecoveryObservation(
            trace_id=trace_id,
            tenant_id=tenant_id,
            job_id=job_id,
            attempt_count=3,
            status="queued",
            error_code="lease_expired",
        ),
    )
    observer = RecordingObserver()
    worker = AgentWorker(
        _SessionFactory(),
        settings=_settings(),
        observer=observer,
    )

    assert await worker.serve_once() is False
    events = [
        event
        for event in observer.events
        if event.event
        in {
            EventName.APPROVAL_EXPIRED,
            EventName.WORKER_JOB_LEASE_RECOVERED,
        }
    ]
    assert [event.event for event in events] == [
        EventName.APPROVAL_EXPIRED,
        EventName.WORKER_JOB_LEASE_RECOVERED,
    ]
    assert all(event.trace_id == trace_id for event in events)
    assert events[0].duration_ms == 4000
    assert events[1].attempt_count == 3
    assert events[1].worker_error_code is WorkerErrorCode.LEASE_EXPIRED
    assert all("source" not in event.model_dump_json() for event in events)


@pytest.mark.asyncio
async def test_serve_once_expires_due_approvals_before_claiming_jobs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_repository(monkeypatch)
    worker = AgentWorker(
        _SessionFactory(),
        runtime_factory=_RuntimeFactory(
            _Runtime(
                _result(
                    status=RunStatus.COMPLETED,
                    reason=StopReason.FINAL,
                    final_response="done",
                )
            )
        ),
        settings=_settings(),
    )

    assert await worker.serve_once() is False
    assert _WORKER_CALLS == ["expire_due", "recover_expired_leases", "claim_next"]
    approval_repository = _FakeApprovalRepository.instances[-1]
    assert approval_repository.calls[0][0] == "expire_due"
    assert approval_repository.calls[0][1]["now"] == _FakeRepository.instances[-1].now


@pytest.mark.asyncio
async def test_persist_result_keeps_awaiting_approval_job_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_repository(monkeypatch)
    claimed = replace(_claimed(), status="awaiting_approval")
    _FakeRepository.next_claimed = claimed
    worker = AgentWorker(_SessionFactory(), settings=_settings())

    async def no_side_effect(_: ClaimedJob) -> bool:
        return False

    monkeypatch.setattr(worker, "_has_side_effect", no_side_effect)
    await worker._persist_result(
        claimed,
        _result(
            status=RunStatus.COMPLETED,
            reason=StopReason.FINAL,
            final_response="approval requested",
        ),
    )

    repository = _FakeRepository.instances[-1]
    assert [name for name, _ in repository.calls] == []


@pytest.mark.asyncio
async def test_completed_executor_stopped_is_succeeded_and_failed_result_is_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_repository(monkeypatch)
    _FakeRepository.next_claimed = _claimed()
    runtime = _Runtime(
        _result(
            status=RunStatus.COMPLETED,
            reason=StopReason.EXECUTOR_STOPPED,
            final_response="stopped by policy",
        )
    )
    worker = AgentWorker(
        _SessionFactory(),
        runtime_factory=_RuntimeFactory(runtime),
        settings=_settings(),
        to_thread=asyncio.to_thread,
    )
    assert await worker.serve_once() is True
    repository = _FakeRepository.instances[-1]
    assert [name for name, _ in repository.calls].count("mark_succeeded") == 1

    _install_repository(monkeypatch)
    _FakeRepository.next_claimed = _claimed()
    failed_runtime = _Runtime(
        _result(
            status=RunStatus.FAILED,
            reason=StopReason.MAX_STEPS,
            final_response=None,
        )
    )
    failed_worker = AgentWorker(
        _SessionFactory(),
        runtime_factory=_RuntimeFactory(failed_runtime),
        settings=_settings(),
        to_thread=asyncio.to_thread,
    )
    assert await failed_worker.serve_once() is True
    failed_repo = _FakeRepository.instances[-1]
    failed = [entry for entry in failed_repo.calls if entry[0] == "mark_failed"]
    assert failed
    assert failed[-1][1][0][1] == "agent_failed"  # type: ignore[index]


@pytest.mark.asyncio
async def test_tool_execution_failure_persists_stable_error_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_repository(monkeypatch)
    _FakeRepository.next_claimed = _claimed()
    runtime = _Runtime(
        _result(
            status=RunStatus.FAILED,
            reason=StopReason.TOOL_EXECUTION_FAILED,
            final_response="tool_execution_failed",
        )
    )
    worker = AgentWorker(
        _SessionFactory(),
        runtime_factory=_RuntimeFactory(runtime),
        settings=_settings(),
        to_thread=asyncio.to_thread,
    )

    assert await worker.serve_once() is True

    repository = _FakeRepository.instances[-1]
    failed = [entry for entry in repository.calls if entry[0] == "mark_failed"]
    assert failed
    assert failed[-1][1][0][1] == "tool_execution_failed"  # type: ignore[index]


@pytest.mark.asyncio
async def test_provider_client_error_is_non_retryable_and_source_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_repository(monkeypatch)
    claimed = _claimed()
    _FakeRepository.next_claimed = claimed
    observer = RecordingObserver()
    worker = AgentWorker(
        _SessionFactory(),
        runtime_factory=_RuntimeFactory(_Runtime(_http_status_error(400))),
        settings=_settings(),
        observer=observer,
        to_thread=asyncio.to_thread,
    )

    assert await worker.serve_once() is True

    repository = _FakeRepository.instances[-1]
    failed = [entry for entry in repository.calls if entry[0] == "mark_failed"]
    assert failed[-1][1][0][1] == "provider_request_rejected"  # type: ignore[index]
    assert not [entry for entry in repository.calls if entry[0] == "schedule_retry"]
    rendered = "".join(event.model_dump_json() for event in observer.events)
    assert "provider-body-secret" not in rendered


@pytest.mark.parametrize("status_code", [408, 429, 500, 503])
@pytest.mark.asyncio
async def test_transient_provider_http_error_is_scheduled_for_retry(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
) -> None:
    _install_repository(monkeypatch)
    _FakeRepository.next_claimed = _claimed()
    worker = AgentWorker(
        _SessionFactory(),
        runtime_factory=_RuntimeFactory(_Runtime(_http_status_error(status_code))),
        settings=_settings(),
        random_uniform=lambda _lower, _upper: 0.0,
        to_thread=asyncio.to_thread,
    )

    assert await worker.serve_once() is True
    repository = _FakeRepository.instances[-1]
    retry = [entry for entry in repository.calls if entry[0] == "schedule_retry"]
    assert retry[-1][1][0][1] == "provider_unavailable"  # type: ignore[index]


@pytest.mark.asyncio
async def test_provider_rejection_after_side_effect_is_failed_uncertain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_repository(monkeypatch)
    _FakeRepository.next_claimed = _claimed(
        side_effect_committed_at=datetime.now(timezone.utc)
    )
    worker = AgentWorker(
        _SessionFactory(),
        runtime_factory=_RuntimeFactory(_Runtime(_http_status_error(400))),
        settings=_settings(),
        to_thread=asyncio.to_thread,
    )

    assert await worker.serve_once() is True
    repository = _FakeRepository.instances[-1]
    uncertain = [
        entry
        for entry in repository.calls
        if entry[0] == "mark_failed_uncertain"
    ]
    assert uncertain[-1][1][0][1] == "unexpected_after_side_effect"  # type: ignore[index]
    assert not [entry for entry in repository.calls if entry[0] == "schedule_retry"]


@pytest.mark.asyncio
async def test_retryable_pre_side_effect_error_is_scheduled_for_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_repository(monkeypatch)
    claimed = _claimed()
    _FakeRepository.next_claimed = claimed
    error = RetryableJobError("provider unavailable")
    runtime = _Runtime(error)
    worker = AgentWorker(
        _SessionFactory(),
        runtime_factory=_RuntimeFactory(runtime),
        settings=_settings(),
        observer=RecordingObserver(),
        random_uniform=lambda _lower, _upper: 0.0,
        to_thread=asyncio.to_thread,
    )
    assert await worker.serve_once() is True
    repository = _FakeRepository.instances[-1]
    retry = [entry for entry in repository.calls if entry[0] == "schedule_retry"]
    assert retry
    assert retry[-1][1][0][0] == claimed.id  # type: ignore[index]
    assert retry[-1][1][0][1] == "provider_unavailable"  # type: ignore[index]

    events = worker._observer.events  # type: ignore[attr-defined]
    scheduled = [
        event for event in events if event.event is EventName.WORKER_JOB_RETRY_SCHEDULED
    ]
    assert len(scheduled) == 1
    assert scheduled[0].trace_id == claimed.trace_id
    assert scheduled[0].attempt_count == claimed.attempt_count
    assert scheduled[0].retry_delay_ms == 500
    assert scheduled[0].worker_error_code is WorkerErrorCode.PROVIDER_UNAVAILABLE


@pytest.mark.asyncio
async def test_retryable_error_after_side_effect_is_failed_uncertain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_repository(monkeypatch)
    claimed = _claimed(side_effect_committed_at=datetime.now(timezone.utc))
    _FakeRepository.next_claimed = claimed
    runtime = _Runtime(RetryableJobError("provider unavailable"))
    worker = AgentWorker(
        _SessionFactory(),
        runtime_factory=_RuntimeFactory(runtime),
        settings=_settings(),
        to_thread=asyncio.to_thread,
    )
    assert await worker.serve_once() is True
    repository = _FakeRepository.instances[-1]
    uncertain = [
        entry for entry in repository.calls if entry[0] == "mark_failed_uncertain"
    ]
    assert uncertain
    assert uncertain[-1][1][0][1] == "side_effect_committed"  # type: ignore[index]
