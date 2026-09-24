"""Offline orchestration tests for the isolated Phase-7 agent worker."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import replace
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.agent.models import AgentMessage, AgentRunResult, MessageRole, RunStatus, StopReason
from app.core.config import Settings
from app.domain.job_repository import ClaimedJob
from app.workers.agent_worker import AgentWorker, RetryableJobError


def _claimed(*, side_effect_committed_at: datetime | None = None) -> ClaimedJob:
    return ClaimedJob(
        id=uuid4(),
        tenant_id=uuid4(),
        status="running",
        source_snapshot={"channel": "email", "subject": "Load", "body": "Need a truck"},
        tenant_config_snapshot={"display_name": "test"},
        tenant_config_sha256="a" * 64,
        risk_signals={"safety_or_legal_risk": False},
        attempt_count=1,
        side_effect_committed_at=side_effect_committed_at,
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

    def __init__(self, session: object) -> None:
        self.calls: list[tuple[str, object]] = []
        _FakeApprovalRepository.instances.append(self)

    async def expire_due(self, **kwargs: object) -> int:
        self.calls.append(("expire_due", kwargs))
        _WORKER_CALLS.append("expire_due")
        return 0


class _FakeRepository:
    instances: list["_FakeRepository"] = []
    next_claimed: ClaimedJob | None = None

    def __init__(self, session: object) -> None:
        self.claimed = _FakeRepository.next_claimed
        _FakeRepository.next_claimed = None
        self.calls: list[tuple[str, object]] = []
        self.now: datetime | None = None
        _FakeRepository.instances.append(self)

    async def recover_expired_leases(self, **kwargs: object) -> int:
        self.calls.append(("recover_expired_leases", kwargs))
        _WORKER_CALLS.append("recover_expired_leases")
        self.now = kwargs["now"]  # type: ignore[assignment]
        return 0

    async def claim_next(self, **kwargs: object) -> ClaimedJob | None:
        self.calls.append(("claim_next", kwargs))
        _WORKER_CALLS.append("claim_next")
        result, self.claimed = self.claimed, None
        return result

    async def mark_succeeded(self, *args: object, **kwargs: object) -> None:
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
    def __init__(self, runtime: _Runtime) -> None:
        self.runtime = runtime
        self.calls: list[tuple[ClaimedJob, object]] = []

    def build(self, claimed: ClaimedJob, gateway: object) -> _Runtime:
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
        "final_response",
        "routing_status",
        "routing_reason",
        "missing_required_fields",
    }
    assert summary == {
        "status": "completed",
        "reason": "executor_stopped",
        "steps": 2,
        "final_response": "document_unreadable",
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
        "final_response": "tool_result_unavailable",
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
        "final_response": None,
    }
    assert "secret document excerpt" not in repr(summary)


def _settings() -> Settings:
    return Settings(
        worker_poll_interval_seconds=0.001,
        worker_lease_seconds=60,
        worker_concurrency=1,
        worker_max_retries=4,
    )


def _install_repository(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeRepository.instances.clear()
    _FakeRepository.next_claimed = None
    _FakeApprovalRepository.instances.clear()
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
    assert set(summary) == {"status", "reason", "steps", "final_response"}
    assert summary == {
        "status": "completed",
        "reason": "final",
        "steps": 2,
        "final_response": "done",
    }


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
        to_thread=asyncio.to_thread,
    )
    assert await worker.serve_once() is True
    repository = _FakeRepository.instances[-1]
    retry = [entry for entry in repository.calls if entry[0] == "schedule_retry"]
    assert retry
    assert retry[-1][1][0][0] == claimed.id  # type: ignore[index]
    assert retry[-1][1][0][1] == "provider_unavailable"  # type: ignore[index]


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
