from __future__ import annotations

from collections.abc import Iterable
from contextlib import AbstractAsyncContextManager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy.dialects import postgresql

from app.auth import AuthenticatedOperator
from app.db.models import Approval, ApprovalEvent
from app.domain.approvals import (
    ApprovalDecisionConflict,
    ApprovalDecisionRequest,
    ApprovalDecisionService,
    ApprovalExpired,
    ApprovalNotFound,
)
from app.tools.models import PendingAction


TENANT_A = UUID("00000000-0000-0000-0000-000000000001")
TENANT_B = UUID("00000000-0000-0000-0000-000000000002")
APPROVAL_ID = UUID("00000000-0000-0000-0000-000000000010")
JOB_ID = UUID("00000000-0000-0000-0000-000000000020")
CASE_ID = UUID("00000000-0000-0000-0000-000000000030")
DECISION_TIME = datetime(2026, 9, 23, 13, tzinfo=timezone.utc)


class _ScalarResult:
    def __init__(self, value: Approval | None) -> None:
        self.value = value

    def scalar_one_or_none(self) -> Approval | None:
        return self.value


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

    def __init__(self, approvals: Iterable[Approval]) -> None:
        self.approvals = list(approvals)
        self.events: list[ApprovalEvent] = []
        self.cases: list[UUID] = []
        self.queries: list[str] = []
        self._transaction_snapshot: dict[str, Any] | None = None

    def begin(self) -> _Transaction:
        return _Transaction(self)

    def add(self, value: ApprovalEvent) -> None:
        self.events.append(value)

    async def execute(self, statement: object) -> _ScalarResult:
        compiled = statement.compile(dialect=postgresql.dialect())
        query = str(compiled)
        self.queries.append(query)
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
        }

    def _rollback(self) -> None:
        snapshot = self._transaction_snapshot
        assert snapshot is not None
        for approval, values in snapshot["approval"]:
            for name, value in values.items():
                setattr(approval, name, value)
        self.events[:] = snapshot["events"]
        self.cases[:] = snapshot["cases"]


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
        known_fields={"summary": "Create a case"},
    )


def _approval(
    *,
    status: str = "pending",
    expires_at: datetime = DECISION_TIME + timedelta(hours=1),
) -> Approval:
    action = _action()
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
        tenant_config_sha256="a" * 64,
        expires_at=expires_at,
        execution_result=None,
    )


def _service(
    session: _ApprovalSession,
    executor: _RecordingExecutor,
    *,
    now: datetime = DECISION_TIME,
) -> ApprovalDecisionService:
    return ApprovalDecisionService(session, executor, clock=lambda: now)


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
    assert [event.event_type for event in session.events] == [
        "approval_approved",
        "approved_action_executed",
    ]

    with pytest.raises(ApprovalDecisionConflict):
        await service.decide(APPROVAL_ID, operator, _request("reject"))
    assert executor.calls == 1
    assert len(session.events) == 2


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
    assert executor.calls == 0
    assert [event.event_type for event in session.events] == ["approval_expired"]


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
