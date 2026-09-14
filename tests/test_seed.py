from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql

from app.auth import hash_api_key
from app.db.models import ApiKey, CaseEvent, IntakeCase, Tenant
from app.db.session import get_db_session
from app.main import app
import scripts.seed_demo as seed_demo
from scripts.seed_demo import DEMO_TENANT_SLUG, seed_demo_tenant


@dataclass
class ScalarResult:
    value: Tenant | IntakeCase | None

    def scalar_one_or_none(self) -> Tenant | IntakeCase | None:
        return self.value


@dataclass
class ListResult:
    values: list[ApiKey]

    def scalars(self) -> "ListResult":
        return self

    def all(self) -> list[ApiKey]:
        return self.values


class Transaction(AbstractAsyncContextManager["Transaction"]):
    def __init__(self, session: "SeedSession") -> None:
        self.session = session

    async def __aenter__(self) -> "Transaction":
        self.session.in_transaction = True
        return self

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        self.session.in_transaction = False
        if exc_type is None:
            self.session.database.commit_pending()
        self.session.pending_tenants.clear()
        self.session.pending_keys.clear()
        self.session.pending_cases.clear()
        self.session.pending_events.clear()


class SeedSession:
    def __init__(self, database: "SeedDatabase") -> None:
        self.database = database
        self.in_transaction = False
        self.pending_tenants: list[Tenant] = []
        self.pending_keys: list[ApiKey] = []
        self.pending_cases: list[IntakeCase] = []
        self.pending_events: list[CaseEvent] = []

    def begin(self) -> Transaction:
        return Transaction(self)

    def add(self, instance: Tenant | ApiKey | IntakeCase | CaseEvent) -> None:
        if isinstance(instance, Tenant):
            self.pending_tenants.append(instance)
        elif isinstance(instance, ApiKey):
            self.pending_keys.append(instance)
        elif isinstance(instance, IntakeCase):
            self.pending_cases.append(instance)
        else:
            self.pending_events.append(instance)

    async def flush(self) -> None:
        for tenant in self.pending_tenants:
            if tenant.id is None:
                tenant.id = uuid4()
        for case in self.pending_cases:
            if case.id is None:
                case.id = uuid4()
            if case.created_at is None:
                case.created_at = datetime.now(timezone.utc)

    async def execute(self, statement: object) -> ScalarResult | ListResult:
        compiled = statement.compile(dialect=postgresql.dialect())
        query = str(compiled)

        if "FROM tenants JOIN api_keys" in query:
            key_hash = compiled.params["key_hash_1"]
            key = next(
                (
                    candidate
                    for candidate in self.database.api_keys
                    if candidate.key_hash == key_hash and candidate.is_active
                ),
                None,
            )
            tenant = (
                next((candidate for candidate in self.database.tenants if candidate.id == key.tenant_id), None)
                if key is not None
                else None
            )
            return ScalarResult(tenant)

        if "FROM tenants" in query:
            return ScalarResult(
                next(
                    (tenant for tenant in self.database.tenants if tenant.slug == compiled.params["slug_1"]),
                    None,
                )
            )

        if "FROM api_keys" in query:
            tenant_id = compiled.params["tenant_id_1"]
            return ListResult(
                [
                    key
                    for key in self.database.api_keys
                    if key.tenant_id == tenant_id and key.is_active
                ]
            )

        assert "FROM intake_cases" in query
        return ScalarResult(None)


class SeedDatabase:
    def __init__(self) -> None:
        self.tenants: list[Tenant] = []
        self.api_keys: list[ApiKey] = []
        self.cases: list[IntakeCase] = []
        self.events: list[CaseEvent] = []

    def commit_pending(self) -> None:
        for tenant in self._session.pending_tenants:
            if tenant.id is None:
                tenant.id = uuid4()
        self.tenants.extend(self._session.pending_tenants)
        self.api_keys.extend(self._session.pending_keys)
        self.cases.extend(self._session.pending_cases)
        self.events.extend(self._session.pending_events)

    def session(self) -> SeedSession:
        session = SeedSession(self)
        self._session = session
        return session


async def test_seed_creates_demo_tenant_rotates_digest_only_key_and_authenticates_case_creation() -> None:
    """Replacing digest persistence or rotation breaks this provisioning-to-request flow."""
    database = SeedDatabase()

    first_raw_key = await seed_demo_tenant(database.session())
    second_raw_key = await seed_demo_tenant(database.session())

    assert len(database.tenants) == 1
    assert database.tenants[0].slug == DEMO_TENANT_SLUG
    assert database.tenants[0].name == "Demo tenant"
    assert database.tenants[0].status == "active"
    assert len(database.api_keys) == 2
    assert database.api_keys[0].is_active is False
    assert database.api_keys[1].is_active is True
    assert database.api_keys[1].key_hash == hash_api_key(second_raw_key)
    assert database.api_keys[1].prefix == second_raw_key[:11]
    assert first_raw_key != second_raw_key
    assert all(raw_key not in key.key_hash for raw_key in (first_raw_key, second_raw_key) for key in database.api_keys)

    async def override_session() -> AsyncGenerator[SeedSession, None]:
        yield database.session()

    app.dependency_overrides[get_db_session] = override_session
    try:
        with TestClient(app) as client:
            response = client.post(
                "/v1/cases",
                headers={"X-API-Key": second_raw_key},
                json={"channel": "email", "subject": "Seeded", "body": "Authenticated"},
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 201
    assert response.json()["source"] == "email"
    assert response.json()["created_at"]


async def test_seed_cli_prints_the_raw_key_once_after_the_transaction_commits(monkeypatch) -> None:
    """Printing before commit would expose a credential that the database rejected."""
    database = SeedDatabase()
    printed_after_commit: list[tuple[str, bool]] = []

    class SessionContext:
        async def __aenter__(self) -> SeedSession:
            return database.session()

        async def __aexit__(self, exc_type, exc_value, traceback) -> None:
            return None

    monkeypatch.setattr(seed_demo, "async_session_factory", lambda: SessionContext())

    await seed_demo.main(lambda raw_key: printed_after_commit.append((raw_key, bool(database.api_keys))))

    assert len(printed_after_commit) == 1
    raw_key, committed = printed_after_commit[0]
    assert raw_key.startswith("ik_")
    assert committed is True
    assert database.api_keys[0].key_hash == hash_api_key(raw_key)
