from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql

from app.auth import generate_api_key
from app.db.models import ApiKey, Approval, ApprovalEvent, Tenant
from app.db.session import get_db_session
from app.main import app
from app.tools.models import PendingAction


TENANT_A = UUID("00000000-0000-0000-0000-000000000001")
TENANT_B = UUID("00000000-0000-0000-0000-000000000002")
CASE_ID = UUID("00000000-0000-0000-0000-000000000030")
NOW = datetime(2026, 9, 23, 13, tzinfo=timezone.utc)


@dataclass
class _Result:
    value: ApiKey | Approval | None

    def scalar_one_or_none(self) -> ApiKey | Approval | None:
        return self.value


class _Transaction(AbstractAsyncContextManager["_Transaction"]):
    async def __aenter__(self) -> "_Transaction":
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None


class _Database:
    def __init__(self) -> None:
        self.tenants: dict[UUID, Tenant] = {}
        self.api_keys: list[ApiKey] = []
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
    ) -> Approval:
        action = PendingAction(
            name="create_case",
            arguments={"customer_id": None},
            known_fields={"summary": "Create a case"},
        )
        approval = Approval(
            id=uuid4(),
            tenant_id=tenant_id,
            case_id=None,
            job_id=uuid4(),
            action=action.name,
            status="pending",
            decision={},
            policy_reason="approval_required",
            pending_action=action.model_dump(mode="json"),
            tenant_config_sha256="a" * 64,
            expires_at=expires_at,
            execution_result=None,
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

        assert "FROM approvals" in query
        approval_id = params["id_1"]
        tenant_id = params["tenant_id_1"]
        return _Result(
            next(
                (
                    approval
                    for approval in self.database.approvals
                    if approval.id == approval_id and approval.tenant_id == tenant_id
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
