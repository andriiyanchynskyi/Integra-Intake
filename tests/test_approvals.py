from __future__ import annotations

from collections.abc import Iterable
from contextlib import AbstractAsyncContextManager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import yaml
from sqlalchemy.dialects import postgresql

from app.auth import AuthenticatedOperator
from app.db.models import AgentJob, Approval, ApprovalEvent
from app.domain.approvals import (
    ApprovalDecisionConflict,
    ApprovalDecisionRequest,
    ApprovalDecisionService,
    ApprovalExpired,
    ApprovalNotFound,
)
from app.observability import EventName, OutcomeCode, RecordingObserver
from app.runtime.profiles import canonical_json_bytes
from app.tools.models import PendingAction


TENANT_A = UUID("00000000-0000-0000-0000-000000000001")
TENANT_B = UUID("00000000-0000-0000-0000-000000000002")
APPROVAL_ID = UUID("00000000-0000-0000-0000-000000000010")
JOB_ID = UUID("00000000-0000-0000-0000-000000000020")
CASE_ID = UUID("00000000-0000-0000-0000-000000000030")
TRACE_ID = UUID("00000000-0000-0000-0000-000000000040")
DECISION_TIME = datetime(2026, 9, 23, 13, tzinfo=timezone.utc)
REPAIR_PROFILE_SNAPSHOT = yaml.safe_load(
    (Path(__file__).parents[1] / "examples" / "repair-service.yaml").read_text(
        encoding="utf-8"
    )
)
REPAIR_PROFILE_SHA256 = hashlib.sha256(
    canonical_json_bytes(REPAIR_PROFILE_SNAPSHOT)
).hexdigest()
CURRENT_PROFILE_SHA256 = hashlib.sha256(
    canonical_json_bytes(
        {
            **REPAIR_PROFILE_SNAPSHOT,
            "display_name": "Changed repair service profile",
        }
    )
).hexdigest()


def _agent_evidence() -> dict[str, object]:
    return {
        "steps": 1,
        "reason": "executor_stopped",
        "execution_path": [
            {
                "step": 1,
                "action_key": "create_case",
                "action_known": True,
                "policy_decision": "needs_approval",
                "routing_status": "urgent",
                "policy_reason": "approval_required",
                "missing_required_fields": [],
                "tool_outcome": None,
                "side_effect_committed": False,
            }
        ],
        "routing_status": "urgent",
        "routing_reason": "approval_required",
        "missing_required_fields": [],
    }


class _ScalarResult:
    def __init__(self, value: object) -> None:
        self.value = value

    def scalar_one_or_none(self) -> object:
        return self.value


class _RowResult:
    def __init__(self, row: tuple[object, ...] | None) -> None:
        self.row = row

    def one_or_none(self) -> tuple[object, ...] | None:
        return self.row


class _Transaction(AbstractAsyncContextManager["_Transaction"]):
    def __init__(self, session: "_ApprovalSession") -> None:
        self.session = session

    async def __aenter__(self) -> "_Transaction":
        self.session._snapshot()
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        if exc_type is not None:
            self.session._rollback()
        self.session._transaction_snapshot = None


class _ApprovalSession:
    """Async-session double for locked approval lookup and transaction rollback."""

    def __init__(
        self,
        approvals: Iterable[Approval],
        *,
        profile_snapshot: dict[str, object] | None = None,
        profile_sha256: str | None = None,
    ) -> None:
        self.approvals = list(approvals)
        self.profile_snapshot = deepcopy(profile_snapshot or REPAIR_PROFILE_SNAPSHOT)
        self.profile_sha256 = profile_sha256 or REPAIR_PROFILE_SHA256
        self.jobs = [
            AgentJob(
                id=approval.job_id,
                tenant_id=approval.tenant_id,
                trace_id=TRACE_ID,
                status="awaiting_approval",
                source_snapshot={
                    "channel": "email",
                    "subject": "Approval",
                    "body": "Approval body",
                },
                tenant_config_snapshot=deepcopy(self.profile_snapshot),
                tenant_config_sha256=self.profile_sha256,
                risk_signals={},
                attempt_count=1,
                available_at=DECISION_TIME,
                result={
                    "status": "completed",
                    "business_outcome": "approval_requested",
                    **_agent_evidence(),
                },
            )
            for approval in self.approvals
            if approval.job_id is not None
        ]
        self.events: list[ApprovalEvent] = []
        self.cases: list[UUID] = []
        self.queries: list[str] = []
        self._transaction_snapshot: dict[str, Any] | None = None

    def begin(self) -> _Transaction:
        return _Transaction(self)

    def add(self, value: ApprovalEvent) -> None:
        self.events.append(value)

    async def execute(self, statement: object) -> _ScalarResult | _RowResult:
        compiled = statement.compile(dialect=postgresql.dialect())
        query = str(compiled)
        self.queries.append(query)
        if "FROM agent_jobs" in query:
            params = compiled.params
            job_id = params["id_1"]
            tenant_id = params["tenant_id_1"]
            job = next(
                (
                    value
                    for value in self.jobs
                    if value.id == job_id and value.tenant_id == tenant_id
                ),
                None,
            )
            if "agent_jobs.status" in query:
                return _ScalarResult(job)
            approval = next(
                (
                    value
                    for value in self.approvals
                    if value.job_id == job_id and value.tenant_id == tenant_id
                ),
                None,
            )
            return _RowResult(
                (
                    TRACE_ID,
                    deepcopy(self.profile_snapshot),
                    self.profile_sha256,
                )
                if approval is not None
                else None
            )
        assert "FROM approvals" in query
        assert "FOR UPDATE" in query.upper()
        params = compiled.params
        approval_id = params["id_1"]
        tenant_id = params["tenant_id_1"]
        return _ScalarResult(
            next(
                (
                    approval
                    for approval in self.approvals
                    if approval.id == approval_id and approval.tenant_id == tenant_id
                ),
                None,
            )
        )

    def _snapshot(self) -> None:
        self._transaction_snapshot = {
            "approval": [
                (
                    approval,
                    {
                        name: deepcopy(getattr(approval, name))
                        for name in (
                            "case_id",
                            "status",
                            "decision",
                            "decided_at",
                            "decided_by_actor_ref",
                            "decision_reason",
                            "executed_at",
                            "execution_result",
                        )
                    },
                )
                for approval in self.approvals
            ],
            "events": list(self.events),
            "cases": list(self.cases),
            "jobs": [
                (
                    job,
                    {
                        name: deepcopy(getattr(job, name))
                        for name in (
                            "status",
                            "finished_at",
                            "lease_expires_at",
                            "result",
                            "error_code",
                            "side_effect_committed_at",
                        )
                    },
                )
                for job in self.jobs
            ],
        }

    def _rollback(self) -> None:
        snapshot = self._transaction_snapshot
        assert snapshot is not None
        for approval, values in snapshot["approval"]:
            for name, value in values.items():
                setattr(approval, name, value)
        self.events[:] = snapshot["events"]
        self.cases[:] = snapshot["cases"]
        for job, values in snapshot["jobs"]:
            for name, value in values.items():
                setattr(job, name, value)


class _RecordingExecutor:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    async def execute(
        self,
        session: _ApprovalSession,
        approval: Approval,
        action: PendingAction,
        *,
        now: datetime,
    ) -> dict[str, object]:
        self.calls += 1
        assert action.name == approval.action
        if self.fail:
            approval.case_id = CASE_ID
            session.cases.append(CASE_ID)
            raise RuntimeError("executor failure must not leak")
        approval.case_id = CASE_ID
        return {
            "case_id": str(CASE_ID),
            "status": "received",
            "executed_at": now.isoformat(),
        }


def _operator(tenant_id: UUID = TENANT_A) -> AuthenticatedOperator:
    return AuthenticatedOperator(
        tenant_id=tenant_id,
        actor_ref="ops-alice",
        credential_id=uuid4(),
    )


def _action() -> PendingAction:
    return PendingAction(
        name="create_case",
        arguments={"customer_id": None},
        known_fields={"customer_name": "Create a case"},
    )


def _approval(
    *,
    status: str = "pending",
    expires_at: datetime = DECISION_TIME + timedelta(hours=1),
    tenant_config_sha256: str = REPAIR_PROFILE_SHA256,
    known_fields: dict[str, object] | None = None,
) -> Approval:
    action = _action()
    if known_fields is not None:
        action = action.model_copy(update={"known_fields": known_fields})
    return Approval(
        id=APPROVAL_ID,
        tenant_id=TENANT_A,
        case_id=None,
        job_id=JOB_ID,
        action=action.name,
        status=status,
        decision={},
        policy_reason="approval_required",
        pending_action=action.model_dump(mode="json"),
        tenant_config_sha256=tenant_config_sha256,
        expires_at=expires_at,
        execution_result=None,
    )


def _service(
    session: _ApprovalSession,
    executor: _RecordingExecutor,
    *,
    now: datetime = DECISION_TIME,
    observer: RecordingObserver | None = None,
    request_id: UUID | None = None,
) -> ApprovalDecisionService:
    return ApprovalDecisionService(
        session,
        executor,
        clock=lambda: now,
        observer=observer or RecordingObserver(),
        request_id=request_id,
    )


def _request(decision: str, reason: str = "Reviewed by operations") -> ApprovalDecisionRequest:
    return ApprovalDecisionRequest(decision=decision, reason=reason)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_reject_records_decision_without_invoking_action_executor() -> None:
    approval = _approval()
    session = _ApprovalSession([approval])
    executor = _RecordingExecutor()

    result = await _service(session, executor).decide(
        APPROVAL_ID,
        _operator(),
        _request("reject"),
    )

    assert result.status == "rejected"
    assert result.executed is False
    assert result.case_id is None
    assert executor.calls == 0
    job = session.jobs[0]
    assert job.status == "succeeded"
    assert job.finished_at == DECISION_TIME
    assert job.error_code is None
    assert job.result == {
        **_agent_evidence(),
        "business_outcome": "approval_rejected",
        "approval_id": str(APPROVAL_ID),
        "approval_status": "rejected",
        "result_reference": {},
    }
    assert approval.decided_by_actor_ref == "ops-alice"
    assert approval.decision_reason == "Reviewed by operations"
    assert [event.event_type for event in session.events] == ["approval_rejected"]


@pytest.mark.asyncio
async def test_approve_executes_create_case_once_and_retries_are_idempotent() -> None:
    approval = _approval()
    session = _ApprovalSession([approval])
    executor = _RecordingExecutor()
    service = _service(session, executor)
    operator = _operator()
    request = _request("approve")

    first = await service.decide(APPROVAL_ID, operator, request)
    second = await service.decide(APPROVAL_ID, operator, request)

    assert first.status == "approved"
    assert first.executed is True
    assert first.case_id == CASE_ID
    assert second == first
    assert executor.calls == 1
    assert approval.status == "approved"
    assert approval.executed_at == DECISION_TIME
    job = session.jobs[0]
    assert job.status == "succeeded"
    assert job.finished_at == DECISION_TIME
    assert job.result == {
        **_agent_evidence(),
        "business_outcome": "tool_result",
        "approval_id": str(APPROVAL_ID),
        "approval_status": "approved",
        "result_reference": {"case_id": str(CASE_ID)},
    }
    assert [event.event_type for event in session.events] == [
        "approval_approved",
        "approved_action_executed",
    ]

    with pytest.raises(ApprovalDecisionConflict):
        await service.decide(APPROVAL_ID, operator, _request("reject"))
    assert executor.calls == 1
    assert len(session.events) == 2


@pytest.mark.asyncio
async def test_approve_rechecks_frozen_profile_authorization_and_fields() -> None:
    denied_snapshot = deepcopy(REPAIR_PROFILE_SNAPSHOT)
    denied_snapshot["action_policy"]["create_case"]["allowed"] = False  # type: ignore[index]
    denied_sha256 = hashlib.sha256(
        canonical_json_bytes(denied_snapshot)
    ).hexdigest()
    approval = _approval(tenant_config_sha256=denied_sha256)
    session = _ApprovalSession(
        [approval],
        profile_snapshot=denied_snapshot,
        profile_sha256=denied_sha256,
    )
    executor = _RecordingExecutor()

    with pytest.raises(ApprovalDecisionConflict, match="no longer authorized"):
        await _service(session, executor).decide(
            APPROVAL_ID,
            _operator(),
            _request("approve"),
        )
    assert executor.calls == 0
    assert session.jobs[0].status == "awaiting_approval"
    assert approval.status == "pending"

    invalid_fields_approval = _approval(known_fields={"not_a_profile_field": "x"})
    invalid_session = _ApprovalSession([invalid_fields_approval])
    invalid_executor = _RecordingExecutor()
    with pytest.raises(ApprovalDecisionConflict, match="known fields"):
        await _service(invalid_session, invalid_executor).decide(
            APPROVAL_ID,
            _operator(),
            _request("approve"),
        )
    assert invalid_executor.calls == 0
    assert invalid_fields_approval.status == "pending"


@pytest.mark.asyncio
async def test_approval_observations_use_original_job_trace_and_redact_decision_data() -> None:
    approval = _approval()
    approval.created_at = DECISION_TIME - timedelta(seconds=2)
    session = _ApprovalSession([approval])
    observer = RecordingObserver()
    request_id = UUID("00000000-0000-0000-0000-000000000041")
    result = await _service(
        session,
        _RecordingExecutor(),
        observer=observer,
        request_id=request_id,
    ).decide(
        APPROVAL_ID,
        _operator(),
        _request("approve", "DECISION_REASON_SECRET"),
    )

    assert result.status == "approved"
    events = [
        event
        for event in observer.events
        if event.event
        in {
            EventName.APPROVAL_DECIDED,
            EventName.APPROVAL_ACTION_COMPLETED,
        }
    ]
    assert [event.event for event in events] == [
        EventName.APPROVAL_DECIDED,
        EventName.APPROVAL_ACTION_COMPLETED,
    ]
    assert [event.outcome for event in events] == [
        OutcomeCode.APPROVED,
        OutcomeCode.EXECUTED,
    ]
    assert all(event.trace_id == TRACE_ID for event in events)
    assert all(event.scenario_key == "repair_service" for event in events)
    assert all(event.profile_fingerprint == REPAIR_PROFILE_SHA256 for event in events)
    assert REPAIR_PROFILE_SHA256 != CURRENT_PROFILE_SHA256
    assert all(event.request_id == request_id for event in events)
    assert all(event.action_key == "create_case" for event in events)
    assert all(event.action_known is True for event in events)
    assert events[0].duration_ms == events[1].duration_ms == 2000
    assert all("DECISION_REASON_SECRET" not in event.model_dump_json() for event in events)
    assert all("pending_action" not in event.model_dump_json() for event in events)


@pytest.mark.asyncio
async def test_executor_failure_rolls_back_decision_case_and_audit() -> None:
    approval = _approval()
    session = _ApprovalSession([approval])
    executor = _RecordingExecutor(fail=True)

    with pytest.raises(RuntimeError, match="executor failure"):
        await _service(session, executor).decide(
            APPROVAL_ID,
            _operator(),
            _request("approve"),
        )

    assert executor.calls == 1
    assert approval.status == "pending"
    assert approval.case_id is None
    assert approval.decided_at is None
    assert approval.executed_at is None
    assert approval.decision == {}
    assert session.cases == []
    assert session.events == []


@pytest.mark.asyncio
async def test_expired_reject_is_terminal_and_never_executes_action() -> None:
    approval = _approval(expires_at=DECISION_TIME - timedelta(seconds=1))
    session = _ApprovalSession([approval])
    executor = _RecordingExecutor()

    result = await _service(session, executor).decide(
        APPROVAL_ID,
        _operator(),
        _request("reject", "Too late to approve"),
    )

    assert result.status == "expired"
    assert result.executed is False
    assert executor.calls == 0
    assert approval.decided_by_actor_ref == "system_timeout"
    assert approval.decision_reason == "approval_timeout"
    assert session.jobs[0].status == "failed"
    assert session.jobs[0].finished_at == DECISION_TIME
    assert session.jobs[0].error_code == "approval_expired"
    assert session.jobs[0].result == {
        **_agent_evidence(),
        "business_outcome": "approval_expired",
        "approval_id": str(APPROVAL_ID),
        "approval_status": "expired",
        "result_reference": {},
    }
    assert [event.event_type for event in session.events] == ["approval_expired"]


@pytest.mark.asyncio
async def test_expired_approve_transitions_to_expired_and_never_executes_action() -> None:
    approval = _approval(expires_at=DECISION_TIME - timedelta(seconds=1))
    session = _ApprovalSession([approval])
    executor = _RecordingExecutor()

    with pytest.raises(ApprovalExpired):
        await _service(session, executor).decide(
            APPROVAL_ID,
            _operator(),
            _request("approve"),
        )

    assert approval.status == "expired"
    assert approval.decided_by_actor_ref == "system_timeout"
    assert session.jobs[0].status == "failed"
    assert session.jobs[0].error_code == "approval_expired"
    assert session.jobs[0].result == {
        **_agent_evidence(),
        "business_outcome": "approval_expired",
        "approval_id": str(APPROVAL_ID),
        "approval_status": "expired",
        "result_reference": {},
    }
    assert executor.calls == 0
    assert [event.event_type for event in session.events] == ["approval_expired"]


@pytest.mark.asyncio
async def test_expired_approval_emits_decision_and_timeout_events() -> None:
    approval = _approval(expires_at=DECISION_TIME - timedelta(seconds=1))
    approval.created_at = DECISION_TIME - timedelta(seconds=3)
    observer = RecordingObserver()
    session = _ApprovalSession([approval])

    with pytest.raises(ApprovalExpired):
        await _service(
            session,
            _RecordingExecutor(),
            observer=observer,
        ).decide(
            APPROVAL_ID,
            _operator(),
            _request("approve"),
        )

    events = [
        event
        for event in observer.events
        if event.event
        in {EventName.APPROVAL_DECIDED, EventName.APPROVAL_EXPIRED}
    ]
    assert [event.outcome for event in events] == [
        OutcomeCode.EXPIRED,
        OutcomeCode.EXPIRED,
    ]
    assert all(event.trace_id == TRACE_ID for event in events)
    assert all(event.scenario_key == "repair_service" for event in events)
    assert all(event.profile_fingerprint == REPAIR_PROFILE_SHA256 for event in events)
    assert REPAIR_PROFILE_SHA256 != CURRENT_PROFILE_SHA256
    assert all(event.action_key == "create_case" for event in events)
    assert all(event.action_known is True for event in events)
    assert all(event.duration_ms == 3000 for event in events)


@pytest.mark.asyncio
async def test_approval_lookup_is_tenant_scoped_and_missing_ids_are_indistinguishable() -> None:
    approval = _approval()
    session = _ApprovalSession([approval])
    executor = _RecordingExecutor()

    with pytest.raises(ApprovalNotFound):
        await _service(session, executor).decide(
            APPROVAL_ID,
            _operator(TENANT_B),
            _request("approve"),
        )

    assert executor.calls == 0
    assert approval.status == "pending"
    assert session.events == []
    assert len(session.queries) == 1
