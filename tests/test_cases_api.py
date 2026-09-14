from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql

from app.auth import generate_api_key, hash_api_key
from app.db.models import ApiKey, CaseEvent, IntakeCase, Tenant
from app.db.session import get_db_session
from app.main import app


@dataclass
class ScalarResult:
    value: Tenant | IntakeCase | None

    def scalar_one_or_none(self) -> Tenant | IntakeCase | None:
        return self.value


class Transaction(AbstractAsyncContextManager["Transaction"]):
    def __init__(self, session: "InMemorySession") -> None:
        self.session = session

    async def __aenter__(self) -> "Transaction":
        assert not self.session.auth_query_used, "CaseService must receive a clean session"
        self.session.in_transaction = True
        return self

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        self.session.in_transaction = False
        if exc_type is None:
            self.session.database.cases.extend(self.session.pending_cases)
            self.session.database.events.extend(self.session.pending_events)
        self.session.pending_cases.clear()
        self.session.pending_events.clear()


class InMemorySession:
    def __init__(self, database: "InMemoryDatabase") -> None:
        self.database = database
        self.auth_query_used = False
        self.in_transaction = False
        self.pending_cases: list[IntakeCase] = []
        self.pending_events: list[CaseEvent] = []

    def begin(self) -> Transaction:
        return Transaction(self)

    def add(self, instance: IntakeCase | CaseEvent) -> None:
        if isinstance(instance, IntakeCase):
            self.pending_cases.append(instance)
        else:
            self.pending_events.append(instance)

    async def flush(self) -> None:
        for case in self.pending_cases:
            if case.id is None:
                case.id = uuid4()
            if case.created_at is None:
                case.created_at = datetime.now(timezone.utc)

    async def execute(self, statement: object) -> ScalarResult:
        compiled = statement.compile(dialect=postgresql.dialect())
        query = str(compiled)

        if "FROM tenants JOIN api_keys" in query:
            self.auth_query_used = True
            key_hash = compiled.params["key_hash_1"]
            key = next(
                (
                    key
                    for key in self.database.api_keys
                    if key.key_hash == key_hash and key.is_active
                ),
                None,
            )
            tenant = (
                next((tenant for tenant in self.database.tenants if tenant.id == key.tenant_id), None)
                if key is not None
                else None
            )
            return ScalarResult(tenant)

        assert "FROM intake_cases" in query
        case_id = compiled.params["id_1"]
        tenant_id = compiled.params["tenant_id_1"]
        return ScalarResult(
            next(
                (
                    case
                    for case in self.database.cases
                    if case.id == case_id and case.tenant_id == tenant_id
                ),
                None,
            )
        )


class InMemoryDatabase:
    def __init__(self) -> None:
        self.tenants: list[Tenant] = []
        self.api_keys: list[ApiKey] = []
        self.cases: list[IntakeCase] = []
        self.events: list[CaseEvent] = []

    def add_tenant_with_key(self, name: str) -> tuple[Tenant, str]:
        raw_key, _, key_hash = generate_api_key()
        tenant = Tenant(id=uuid4(), slug=name.lower(), name=name, status="active")
        self.tenants.append(tenant)
        self.api_keys.append(
            ApiKey(
                tenant_id=tenant.id,
                prefix=raw_key[:11],
                key_hash=key_hash,
                is_active=True,
            )
        )
        return tenant, raw_key

    def add_case(self, tenant_id: UUID) -> IntakeCase:
        case = IntakeCase(
            id=uuid4(),
            tenant_id=tenant_id,
            status="received",
            source="email",
            channel="email",
            subject="Need help",
            body="Details",
            raw_payload={"channel": "email", "subject": "Need help", "body": "Details"},
            extracted_fields={"priority": "high"},
        )
        case.created_at = datetime.now(timezone.utc)
        self.cases.append(case)
        return case


@pytest.fixture
def database() -> InMemoryDatabase:
    return InMemoryDatabase()


@pytest.fixture
def client(database: InMemoryDatabase) -> AsyncGenerator[TestClient, None]:
    async def override_session() -> AsyncGenerator[InMemorySession, None]:
        yield InMemorySession(database)

    app.dependency_overrides[get_db_session] = override_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def test_create_case_returns_201_and_a_case_without_tenant_ownership(
    client: TestClient, database: InMemoryDatabase
) -> None:
    """A route that trusts body ownership or reuses the auth session must fail here."""
    _, api_key = database.add_tenant_with_key("Acme")

    response = client.post(
        "/v1/cases",
        headers={"X-API-Key": api_key},
        json={
            "channel": "email",
            "subject": "Password reset",
            "body": "Please reset my account password.",
            "extracted_fields": {"priority": "high"},
        },
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload == {
        "id": str(database.cases[0].id),
        "customer_id": None,
        "status": "received",
        "source": "email",
        "channel": "email",
        "subject": "Password reset",
        "body": "Please reset my account password.",
        "raw_payload": {
            "channel": "email",
            "subject": "Password reset",
            "body": "Please reset my account password.",
        },
        "extracted_fields": {"priority": "high"},
        "created_at": database.cases[0].created_at.isoformat().replace("+00:00", "Z"),
    }
    assert "tenant_id" not in payload


def test_get_case_returns_200_for_its_owning_tenant(
    client: TestClient, database: InMemoryDatabase
) -> None:
    """Dropping the scoped repository lookup would expose cross-tenant case IDs."""
    tenant, api_key = database.add_tenant_with_key("Acme")
    case = database.add_case(tenant.id)

    response = client.get(f"/v1/cases/{case.id}", headers={"X-API-Key": api_key})

    assert response.status_code == 200
    assert response.json()["id"] == str(case.id)
    assert response.json()["channel"] == "email"
    assert response.json()["subject"] == "Need help"
    assert response.json()["body"] == "Details"
    assert response.json()["created_at"] == case.created_at.isoformat().replace("+00:00", "Z")
    assert response.json()["raw_payload"]["subject"] == "Need help"
    assert "tenant_id" not in response.json()


@pytest.mark.parametrize("header_value", [None, "ik_" + "A" * 43])
def test_case_routes_reject_missing_or_invalid_api_keys(
    client: TestClient, database: InMemoryDatabase, header_value: str | None
) -> None:
    """Removing the authentication dependency would allow anonymous case access."""
    database.add_tenant_with_key("Acme")
    headers = {} if header_value is None else {"X-API-Key": header_value}

    response = client.get(f"/v1/cases/{uuid4()}", headers=headers)

    assert response.status_code == 401


def test_get_case_returns_404_for_another_tenant(
    client: TestClient, database: InMemoryDatabase
) -> None:
    """Using an unscoped case lookup would turn this tenant-isolation response into 200."""
    owner, _ = database.add_tenant_with_key("Acme")
    _, other_key = database.add_tenant_with_key("Globex")
    case = database.add_case(owner.id)

    response = client.get(f"/v1/cases/{case.id}", headers={"X-API-Key": other_key})

    assert response.status_code == 404


def test_create_case_rejects_a_body_tenant_id(client: TestClient, database: InMemoryDatabase) -> None:
    """Allowing tenant_id in the request would let clients attempt to choose case ownership."""
    _, api_key = database.add_tenant_with_key("Acme")

    response = client.post(
        "/v1/cases",
        headers={"X-API-Key": api_key},
        json={
            "channel": "email",
            "subject": "Password reset",
            "body": "Please reset my account password.",
            "tenant_id": str(uuid4()),
        },
    )

    assert response.status_code == 422
