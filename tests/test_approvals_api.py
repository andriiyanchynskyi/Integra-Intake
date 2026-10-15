from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import AbstractAsyncContextManager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
import hashlib
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql

from app.auth import generate_api_key
from app.db.models import AgentJob, ApiKey, Approval, ApprovalEvent, Tenant
from app.db.session import get_db_session
from app.domain.approval_repository import ApprovalRepository
from app.main import app
from app.tools.models import PendingAction
from app.runtime.profiles import canonical_json_bytes


TENANT_A = UUID("00000000-0000-0000-0000-000000000001")
TENANT_B = UUID("00000000-0000-0000-0000-000000000002")
CASE_ID = UUID("00000000-0000-0000-0000-000000000030")
TRACE_ID = UUID("00000000-0000-0000-0000-000000000040")
NOW = datetime(2026, 9, 23, 13, tzinfo=timezone.utc)
REPAIR_PROFILE_SNAPSHOT = yaml.safe_load(
    (Path(__file__).parents[1] / "examples" / "repair-service.yaml").read_text(
        encoding="utf-8"
    )
)
REPAIR_PROFILE_SHA256 = hashlib.sha256(
    canonical_json_bytes(REPAIR_PROFILE_SNAPSHOT)
).hexdigest()


@dataclass
class _Result:
    value: object

    def scalar_one_or_none(self) -> object:
        return self.value

    def one_or_none(self) -> object:
        return self.value

    def scalars(self) -> "_Result":
        return self

    def all(self) -> list[object]:
        if isinstance(self.value, list):
            return self.value
        return [self.value] if self.value is not None else []


class _Transaction(AbstractAsyncContextManager["_Transaction"]):
    async def __aenter__(self) -> "_Transaction":
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None


class _Database:
    def __init__(self) -> None:
        self.tenants: dict[UUID, Tenant] = {}
        self.api_keys: list[ApiKey] = []
        self.jobs: list[AgentJob] = []
        self.approvals: list[Approval] = []
        self.events: list[ApprovalEvent] = []

    def add_tenant(self, tenant_id: UUID) -> Tenant:
        tenant = Tenant(
            id=tenant_id,
            slug=f"tenant-{tenant_id.int}",
            name=str(tenant_id),
            status="active",
        )
        self.tenants[tenant_id] = tenant
        return tenant

    def add_key(
        self,
        tenant_id: UUID,
        *,
        principal_type: str = "service",
        capability: str = "none",
        actor_ref: str | None = None,
        is_active: bool = True,
    ) -> tuple[str, ApiKey]:
        raw_key, prefix, key_hash = generate_api_key()
        key = ApiKey(
            id=uuid4(),
            tenant_id=tenant_id,
            prefix=prefix,
            key_hash=key_hash,
            is_active=is_active,
            principal_type=principal_type,
            capability=capability,
            actor_ref=actor_ref,
        )
        self.api_keys.append(key)
        return raw_key, key

    def add_approval(
        self,
        tenant_id: UUID,
        *,
        expires_at: datetime = datetime(2099, 1, 1, tzinfo=timezone.utc),
        action_name: str = "create_case",
        stored_action: str | None = None,
        action_version: int = 1,
        arguments: dict[str, object] | None = None,
        with_job: bool = True,
    ) -> Approval:
        action = PendingAction(
            name=action_name,
            version=action_version,
            arguments=arguments or {"customer_id": None},
            known_fields={"customer_name": "Create a case"},
        )
        job_id = uuid4() if with_job else None
        approval = Approval(
            id=uuid4(),
            tenant_id=tenant_id,
            case_id=None,
            job_id=job_id,
            action=stored_action or action.name,
            status="pending",
            decision={},
            policy_reason="approval_required",
            pending_action=action.model_dump(mode="json"),
            tenant_config_sha256=REPAIR_PROFILE_SHA256,
            expires_at=expires_at,
            execution_result=None,
        )
        if job_id is not None:
            self.jobs.append(
                AgentJob(
                    id=job_id,
                    tenant_id=tenant_id,
                    trace_id=TRACE_ID,
                    status="awaiting_approval",
                    source_snapshot={
                        "channel": "email",
                        "subject": "Approval",
                        "body": "Approval body",
                    },
                    tenant_config_snapshot=deepcopy(REPAIR_PROFILE_SNAPSHOT),
                    tenant_config_sha256=REPAIR_PROFILE_SHA256,
                    risk_signals={},
                    attempt_count=1,
                    available_at=NOW,
                )
            )
        self.approvals.append(approval)
        return approval


class _Session:
    def __init__(self, database: _Database) -> None:
        self.database = database

    def begin(self) -> _Transaction:
        return _Transaction()

    def add(self, value: ApprovalEvent) -> None:
        self.database.events.append(value)

    def _has_published_lifecycle(self, approval: Approval) -> bool:
        if approval.job_id is None:
            return approval.status in {"pending", "approved", "rejected", "expired"}
        job = next(
            (
                candidate
                for candidate in self.database.jobs
                if candidate.id == approval.job_id
                and candidate.tenant_id == approval.tenant_id
            ),
            None,
        )
        if job is None:
            return False
        return (
            approval.status == "pending" and job.status == "awaiting_approval"
        ) or (
            approval.status in {"approved", "rejected"}
            and job.status == "succeeded"
        ) or (approval.status == "expired" and job.status == "failed")

    async def execute(self, statement: object) -> _Result:
        compiled = statement.compile(dialect=postgresql.dialect())
        query = str(compiled)
        params = compiled.params

        if "FROM api_keys JOIN tenants" in query:
            key_hash = params["key_hash_1"]
            key = next(
                (
                    candidate
                    for candidate in self.database.api_keys
                    if candidate.key_hash == key_hash
                    and candidate.is_active
                    and candidate.principal_type == "operator"
                    and candidate.capability == "approval_decider"
                    and candidate.actor_ref
                    and self.database.tenants[candidate.tenant_id].status == "active"
                ),
                None,
            )
            return _Result(key)

        if "FROM agent_jobs" in query:
            job_id = params["id_1"]
            tenant_id = params["tenant_id_1"]
            job = next(
                (
                    candidate
                    for candidate in self.database.jobs
                    if candidate.id == job_id and candidate.tenant_id == tenant_id
                ),
                None,
            )
            if "agent_jobs.status" in query:
                return _Result(job)
            approval = next(
                (
                    candidate
                    for candidate in self.database.approvals
                    if candidate.job_id == job_id and candidate.tenant_id == tenant_id
                ),
                None,
            )
            if approval is None:
                return _Result(None)
            if "tenant_config_snapshot" in query:
                return _Result(
                    (
                        TRACE_ID,
                        deepcopy(REPAIR_PROFILE_SNAPSHOT),
                        REPAIR_PROFILE_SHA256,
                    )
                )
            return _Result(TRACE_ID)

        assert "FROM approvals" in query
        if "status_1" in params and "id_1" not in params:
            tenant_id = params.get("tenant_id_1")
            cutoff = next(
                (
                    value
                    for value in params.values()
                    if isinstance(value, datetime)
                ),
                None,
            )
            rows = [
                candidate
                for candidate in self.database.approvals
                if (tenant_id is None or candidate.tenant_id == tenant_id)
                and candidate.status == params["status_1"]
                and self._has_published_lifecycle(candidate)
                and (cutoff is None or candidate.expires_at <= cutoff)
            ]
            rows.sort(key=lambda candidate: (candidate.expires_at, candidate.id))
            return _Result(rows)
        tenant_id = params["tenant_id_1"]
        approval_id = params["id_1"]
        return _Result(
            next(
                (
                    approval
                    for approval in self.database.approvals
                    if approval.id == approval_id and approval.tenant_id == tenant_id
                    and self._has_published_lifecycle(approval)
                ),
                None,
            )
        )


class _ActionExecutor:
    calls = 0

    async def execute(
        self,
        session: _Session,
        approval: Approval,
        action: PendingAction,
        *,
        now: datetime,
    ) -> dict[str, object]:
        type(self).calls += 1
        approval.case_id = CASE_ID
        return {"case_id": str(CASE_ID), "status": "received"}


@pytest.fixture
def database() -> _Database:
    value = _Database()
    value.add_tenant(TENANT_A)
    value.add_tenant(TENANT_B)
    return value


@pytest.fixture
def operator_key(database: _Database) -> str:
    raw_key, _ = database.add_key(
        TENANT_A,
        principal_type="operator",
        capability="approval_decider",
        actor_ref="ops-alice",
    )
    return raw_key


@pytest.fixture
def client(
    database: _Database,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncGenerator[TestClient, None]:
    async def override_session() -> AsyncGenerator[_Session, None]:
        yield _Session(database)

    monkeypatch.setattr("app.api.approvals.PostgresApprovalActionExecutor", _ActionExecutor)
    _ActionExecutor.calls = 0
    app.dependency_overrides[get_db_session] = override_session
    try:
        with TestClient(app) as value:
            yield value
    finally:
        app.dependency_overrides.clear()


def test_operator_can_approve_repeat_idempotently_and_opposite_decision_conflicts(
    client: TestClient,
    database: _Database,
    operator_key: str,
) -> None:
    approval = database.add_approval(TENANT_A)
    headers = {"X-API-Key": operator_key}
    payload = {"decision": "approve", "reason": "Reviewed by operations"}

    first = client.post(f"/v1/approvals/{approval.id}/decide", headers=headers, json=payload)
    repeated = client.post(
        f"/v1/approvals/{approval.id}/decide", headers=headers, json=payload
    )
    opposite = client.post(
        f"/v1/approvals/{approval.id}/decide",
        headers=headers,
        json={"decision": "reject", "reason": "Changed my mind"},
    )

    assert first.status_code == 200
    assert first.json()["status"] == "approved"
    assert first.json()["executed"] is True
    assert first.json()["case_id"] == str(CASE_ID)
    assert repeated.status_code == 200
    assert repeated.json() == first.json()
    assert opposite.status_code == 409
    assert _ActionExecutor.calls == 1
    assert [event.event_type for event in database.events] == [
        "approval_approved",
        "approved_action_executed",
    ]


@pytest.mark.parametrize(
    ("action_name", "stored_action", "action_version", "arguments"),
    (
        ("future_action", None, 1, {}),
        ("create_case", "update_case_fields", 1, {"customer_id": None}),
        ("create_case", None, 2, {"customer_id": None}),
        ("create_case", None, 1, {"unexpected": True}),
    ),
    ids=["unknown_action", "mismatched_action", "pending_version", "invalid_arguments"],
)
def test_invalid_registry_approval_command_fails_before_side_effect(
    client: TestClient,
    database: _Database,
    operator_key: str,
    action_name: str,
    stored_action: str | None,
    action_version: int,
    arguments: dict[str, object],
) -> None:
    approval = database.add_approval(
        TENANT_A,
        action_name=action_name,
        stored_action=stored_action,
        action_version=action_version,
        arguments=arguments,
    )

    response = client.post(
        f"/v1/approvals/{approval.id}/decide",
        headers={"X-API-Key": operator_key},
        json={"decision": "approve", "reason": "Reviewed by operations"},
    )

    assert response.status_code == 409
    assert approval.status == "pending"
    assert approval.case_id is None
    assert _ActionExecutor.calls == 0


def test_service_key_is_unauthorized_and_malformed_key_is_rejected(
    client: TestClient,
    database: _Database,
) -> None:
    approval = database.add_approval(TENANT_A)
    service_key, _ = database.add_key(TENANT_A)

    service_response = client.post(
        f"/v1/approvals/{approval.id}/decide",
        headers={"X-API-Key": service_key},
        json={"decision": "reject", "reason": "No"},
    )
    malformed_response = client.post(
        f"/v1/approvals/{approval.id}/decide",
        headers={"X-API-Key": "not-a-key"},
        json={"decision": "reject", "reason": "No"},
    )

    assert service_response.status_code == 401
    assert malformed_response.status_code == 401
    assert approval.status == "pending"


def test_cross_tenant_approval_id_is_not_found(
    client: TestClient,
    database: _Database,
    operator_key: str,
) -> None:
    approval = database.add_approval(TENANT_B)

    response = client.post(
        f"/v1/approvals/{approval.id}/decide",
        headers={"X-API-Key": operator_key},
        json={"decision": "reject", "reason": "No"},
    )

    assert response.status_code == 404
    assert approval.status == "pending"


def test_expired_approval_maps_to_conflict_and_does_not_execute(
    client: TestClient,
    database: _Database,
    operator_key: str,
) -> None:
    approval = database.add_approval(
        TENANT_A,
        expires_at=NOW - timedelta(seconds=1),
    )

    response = client.post(
        f"/v1/approvals/{approval.id}/decide",
        headers={"X-API-Key": operator_key},
        json={"decision": "approve", "reason": "Too late"},
    )

    assert response.status_code == 409
    assert approval.status == "expired"
    assert _ActionExecutor.calls == 0


@pytest.mark.parametrize(
    "payload",
    [
        {"decision": "approve", "reason": ""},
        {"decision": "unknown", "reason": "No"},
        {"decision": "reject", "reason": "No", "extra": True},
    ],
    ids=["blank_reason", "invalid_decision", "unknown_field"],
)
def test_malformed_decision_payload_returns_422(
    client: TestClient,
    database: _Database,
    operator_key: str,
    payload: dict[str, object],
) -> None:
    approval = database.add_approval(TENANT_A)

    response = client.post(
        f"/v1/approvals/{approval.id}/decide",
        headers={"X-API-Key": operator_key},
        json=payload,
    )

    assert response.status_code == 422
    assert approval.status == "pending"


def test_missing_approval_id_returns_404(
    client: TestClient,
    operator_key: str,
) -> None:
    response = client.post(
        f"/v1/approvals/{uuid4()}/decide",
        headers={"X-API-Key": operator_key},
        json={"decision": "reject", "reason": "No"},
    )

    assert response.status_code == 404


def test_operator_can_read_a_source_free_approval_detail(
    client: TestClient,
    database: _Database,
    operator_key: str,
) -> None:
    approval = database.add_approval(TENANT_A)

    response = client.get(
        f"/v1/approvals/{approval.id}",
        headers={"X-API-Key": operator_key},
    )

    assert response.status_code == 200
    assert response.json() == {
        "approval_id": str(approval.id),
        "job_id": str(approval.job_id),
        "action": "create_case",
        "case_id": None,
        "validated_fields": {"customer_name": "Create a case"},
        "policy_reason": "approval_required",
        "expires_at": approval.expires_at.isoformat().replace("+00:00", "Z"),
        "status": "pending",
        "result_reference": {},
        "executable": True,
        "decided_at": None,
    }
    assert "source_snapshot" not in response.text
    assert "pending_action" not in response.text
    assert "rationale" not in response.text


def test_cross_tenant_approval_detail_is_not_found(
    client: TestClient,
    database: _Database,
    operator_key: str,
) -> None:
    approval = database.add_approval(TENANT_B)

    response = client.get(
        f"/v1/approvals/{approval.id}",
        headers={"X-API-Key": operator_key},
    )

    assert response.status_code == 404


@pytest.mark.parametrize("internal_status", ["staged", "cancelled"])
def test_internal_approval_is_hidden_from_public_read_list_and_decision(
    client: TestClient,
    database: _Database,
    operator_key: str,
    internal_status: str,
) -> None:
    approval = database.add_approval(TENANT_A)
    approval.status = internal_status
    headers = {"X-API-Key": operator_key}

    detail = client.get(f"/v1/approvals/{approval.id}", headers=headers)
    listing = client.get(
        "/v1/approvals",
        headers=headers,
        params={"status": "pending"},
    )
    decision = client.post(
        f"/v1/approvals/{approval.id}/decide",
        headers=headers,
        json={"decision": "reject", "reason": "Not yet published"},
    )

    assert detail.status_code == 404
    assert listing.status_code == 200
    assert listing.json()["items"] == []
    assert decision.status_code == 404
    assert approval.status == internal_status


def test_legacy_approval_is_visible_but_not_executable(
    client: TestClient,
    database: _Database,
    operator_key: str,
) -> None:
    approval = database.add_approval(TENANT_A, with_job=False)
    headers = {"X-API-Key": operator_key}

    detail = client.get(f"/v1/approvals/{approval.id}", headers=headers)
    listing = client.get(
        "/v1/approvals",
        headers=headers,
        params={"status": "pending"},
    )
    decision = client.post(
        f"/v1/approvals/{approval.id}/decide",
        headers=headers,
        json={"decision": "approve", "reason": "Legacy review"},
    )

    assert detail.status_code == 200
    assert detail.json()["job_id"] is None
    assert detail.json()["executable"] is False
    assert listing.status_code == 200
    assert [item["approval_id"] for item in listing.json()["items"]] == [
        str(approval.id)
    ]
    assert decision.status_code == 409
    assert approval.status == "pending"


@pytest.mark.asyncio
async def test_legacy_pending_approval_can_expire() -> None:
    database = _Database()
    database.add_tenant(TENANT_A)
    approval = database.add_approval(
        TENANT_A,
        with_job=False,
        expires_at=NOW - timedelta(seconds=1),
    )

    observations = await ApprovalRepository(_Session(database)).expire_due(now=NOW)

    assert observations == ()
    assert approval.status == "expired"
    assert approval.decided_by_actor_ref == "system_timeout"
    assert [event.event_type for event in database.events] == ["approval_expired"]


def test_approval_list_is_tenant_scoped_ordered_and_bounded(
    client: TestClient,
    database: _Database,
    operator_key: str,
) -> None:
    first = database.add_approval(
        TENANT_A,
        expires_at=NOW + timedelta(minutes=2),
    )
    second = database.add_approval(
        TENANT_A,
        expires_at=NOW + timedelta(minutes=1),
    )
    database.add_approval(TENANT_B, expires_at=NOW)

    response = client.get(
        "/v1/approvals",
        headers={"X-API-Key": operator_key},
        params={"status": "pending", "limit": 1},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["limit"] == 1
    assert payload["has_more"] is True
    assert [item["approval_id"] for item in payload["items"]] == [str(second.id)]
    assert str(first.id) not in response.text or first.id == second.id


def test_malformed_legacy_pending_command_is_safe_and_not_executable(
    client: TestClient,
    database: _Database,
    operator_key: str,
) -> None:
    approval = database.add_approval(TENANT_A)
    approval.pending_action = {"known_fields": {"secret": "source text"}}

    response = client.get(
        f"/v1/approvals/{approval.id}",
        headers={"X-API-Key": operator_key},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["executable"] is False
    assert payload["validated_fields"] == {}
    assert "source text" not in response.text
