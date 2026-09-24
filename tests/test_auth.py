import hashlib
import logging
import time
from collections.abc import AsyncGenerator, Mapping
from dataclasses import dataclass, field
from uuid import UUID, uuid4

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql

from app.auth import (
    API_KEY_FORMAT,
    API_KEY_PREFIX,
    API_KEY_TOKEN_PATTERN,
    AuthenticatedOperator,
    SAFE_PREFIX_LENGTH,
    generate_api_key,
    get_current_inbound_tenant,
    get_current_operator,
    get_current_tenant,
    hash_api_key,
    sign_inbound_webhook,
    verify_inbound_webhook_signature,
)
from app.db.models import Tenant
from app.db.session import get_db_session


@dataclass
class ScalarResult:
    value: Tenant | None

    def scalar_one_or_none(self) -> Tenant | None:
        return self.value


@dataclass
class StoredApiKey:
    key_hash: str
    tenant_id: UUID
    is_active: bool
    tenant: Tenant
    id: UUID = field(default_factory=uuid4)
    principal_type: str = "service"
    capability: str = "none"
    actor_ref: str | None = None


class StatementAwareSession:
    """Unit-level database boundary that accepts only the Task 2 lookup contract."""

    def __init__(self, records: Mapping[str, StoredApiKey]) -> None:
        self.records = records

    async def execute(self, statement: object) -> ScalarResult:
        compiled = statement.compile(dialect=postgresql.dialect())
        query = str(compiled)
        if "FROM api_keys JOIN tenants ON api_keys.tenant_id = tenants.id" in query:
            assert "api_keys.key_hash = %(key_hash_1)s" in query
            assert "api_keys.is_active IS true" in query
            assert "api_keys.principal_type = %(principal_type_1)s" in query
            assert "api_keys.capability = %(capability_1)s" in query
            assert "api_keys.actor_ref IS NOT NULL" in query
            assert "tenants.status = %(status_1)s" in query

            key_hash = compiled.params["key_hash_1"]
            record = self.records.get(key_hash)
            if (
                record is None
                or not record.is_active
                or record.tenant.id != record.tenant_id
                or record.tenant.status != "active"
                or record.principal_type != "operator"
                or record.capability != "approval_decider"
                or not record.actor_ref
            ):
                return ScalarResult(None)
            return ScalarResult(record)

        assert "FROM tenants JOIN api_keys ON api_keys.tenant_id = tenants.id" in query
        assert "api_keys.key_hash = %(key_hash_1)s" in query
        assert "api_keys.is_active IS true" in query

        key_hash = compiled.params["key_hash_1"]
        record = self.records.get(key_hash)
        if record is None or not record.is_active or record.tenant.id != record.tenant_id:
            return ScalarResult(None)
        return ScalarResult(record.tenant)


@pytest.fixture
def seeded_credentials() -> tuple[Tenant, StoredApiKey, str]:
    raw_key, _, key_hash = generate_api_key()
    tenant = Tenant(id=uuid4(), slug="acme", name="Acme", status="active")
    record = StoredApiKey(
        key_hash=key_hash,
        tenant_id=tenant.id,
        is_active=True,
        tenant=tenant,
    )
    return tenant, record, raw_key


@pytest.fixture
def client_factory():
    def build(records: Mapping[str, StoredApiKey]) -> TestClient:
        app = FastAPI()

        @app.get("/tenant")
        async def current_tenant(current: Tenant = Depends(get_current_tenant)) -> dict[str, str]:
            return {"tenant_id": str(current.id)}

        @app.post("/inbound-tenant")
        async def current_inbound_tenant(
            current: Tenant = Depends(get_current_inbound_tenant),
        ) -> dict[str, str]:
            return {"tenant_id": str(current.id)}

        @app.get("/operator")
        async def current_operator(
            current: AuthenticatedOperator = Depends(get_current_operator),
        ) -> dict[str, str]:
            return {
                "tenant_id": str(current.tenant_id),
                "actor_ref": current.actor_ref,
                "credential_id": str(current.credential_id),
            }

        async def override_session() -> AsyncGenerator[StatementAwareSession, None]:
            yield StatementAwareSession(records)

        app.dependency_overrides[get_db_session] = override_session
        return TestClient(app)

    return build


def _signed_webhook_headers(raw_key: str, body: bytes) -> dict[str, str]:
    timestamp = str(int(time.time()))
    return {
        "X-API-Key": raw_key,
        "X-Inbound-Timestamp": timestamp,
        "X-Inbound-Signature": sign_inbound_webhook(raw_key, int(timestamp), body),
    }


def test_hash_api_key_is_deterministic_sha256_digest() -> None:
    raw_key = "ik_" + "A" * 43

    assert hash_api_key(raw_key) == hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def test_inbound_webhook_signature_accepts_the_signed_body_at_current_time() -> None:
    raw_key = "ik_" + "A" * 43
    body = b'{"provider_id":"message-1"}'
    timestamp = 1_700_000_000
    signature = sign_inbound_webhook(raw_key, timestamp, body)

    assert verify_inbound_webhook_signature(
        raw_key,
        str(timestamp),
        signature,
        body,
        now=timestamp,
    )


def test_inbound_webhook_signature_rejects_non_ascii_without_raising() -> None:
    raw_key = "ik_" + "A" * 43

    assert not verify_inbound_webhook_signature(
        raw_key,
        "1700000000",
        "v1=é",
        b"body",
        now=1_700_000_000,
    )


@pytest.mark.parametrize(
    ("timestamp_header", "signature_body", "body"),
    [
        ("1700000000", b'{"provider_id":"message-1"}', b'{"provider_id":"message-2"}'),
        ("1700000001", b'{"provider_id":"message-1"}', b'{"provider_id":"message-1"}'),
    ],
)
def test_inbound_webhook_signature_rejects_changed_body_or_timestamp(
    timestamp_header: str,
    signature_body: bytes,
    body: bytes,
) -> None:
    raw_key = "ik_" + "A" * 43
    original_timestamp = 1_700_000_000
    signature = sign_inbound_webhook(raw_key, original_timestamp, signature_body)

    assert not verify_inbound_webhook_signature(
        raw_key,
        timestamp_header,
        signature,
        body,
        now=original_timestamp,
    )


@pytest.mark.parametrize(
    ("timestamp_header", "signature"),
    [
        ("not-a-timestamp", "v1=invalid"),
        ("1700000000", "not-a-signature"),
        ("1700000000", None),
    ],
)
def test_inbound_webhook_signature_rejects_malformed_headers(
    timestamp_header: str,
    signature: str | None,
) -> None:
    raw_key = "ik_" + "A" * 43

    assert not verify_inbound_webhook_signature(
        raw_key,
        timestamp_header,
        signature,
        b"body",
        now=1_700_000_000,
    )


@pytest.mark.parametrize(
    "now",
    [
        1_700_000_000 + 300 + 1,
        1_700_000_000 - 300 - 1,
    ],
)
def test_inbound_webhook_signature_rejects_stale_and_future_timestamps(now: int) -> None:
    raw_key = "ik_" + "A" * 43
    body = b"body"
    timestamp = 1_700_000_000
    signature = sign_inbound_webhook(raw_key, timestamp, body)

    assert not verify_inbound_webhook_signature(
        raw_key,
        str(timestamp),
        signature,
        body,
        now=now,
    )


def test_generate_api_key_returns_provisioning_credential_and_non_secret_metadata() -> None:
    raw_key, safe_prefix, key_hash = generate_api_key()

    assert API_KEY_FORMAT == "ik_<43 URL-safe characters from secrets.token_urlsafe(32)>"
    assert raw_key.startswith(API_KEY_PREFIX)
    assert API_KEY_TOKEN_PATTERN.fullmatch(raw_key)
    assert safe_prefix == raw_key[:SAFE_PREFIX_LENGTH]
    assert key_hash == hash_api_key(raw_key)
    assert raw_key not in safe_prefix
    assert raw_key not in key_hash


@pytest.mark.parametrize("header_value", [None, "", "wrong-prefix", "ik_"])
def test_missing_or_malformed_api_key_returns_401(client_factory, header_value: str | None) -> None:
    """Rejecting incomplete credentials prevents anonymous tenant access."""
    headers = {} if header_value is None else {"X-API-Key": header_value}

    response = client_factory({}).get("/tenant", headers=headers)

    assert response.status_code == 401


def test_unknown_api_key_returns_401(client_factory, seeded_credentials) -> None:
    """A different well-formed key cannot match the stored digest."""
    _, record, _ = seeded_credentials
    unknown_key, _, _ = generate_api_key()

    response = client_factory({record.key_hash: record}).get(
        "/tenant", headers={"X-API-Key": unknown_key}
    )

    assert response.status_code == 401


def test_inactive_api_key_returns_401(client_factory, seeded_credentials) -> None:
    """A record matching the digest must still be rejected when inactive."""
    _, record, raw_key = seeded_credentials
    record.is_active = False

    response = client_factory({record.key_hash: record}).get(
        "/tenant", headers={"X-API-Key": raw_key}
    )

    assert response.status_code == 401


def test_active_api_key_resolves_the_tenant_joined_to_its_digest(client_factory, seeded_credentials) -> None:
    """The accepted digest resolves only the tenant related by the query join."""
    tenant, record, raw_key = seeded_credentials

    response = client_factory({record.key_hash: record}).get(
        "/tenant", headers={"X-API-Key": raw_key}
    )

    assert response.status_code == 200
    assert response.json() == {"tenant_id": str(tenant.id)}


def test_active_api_key_resolves_the_tenant_for_signed_inbound_webhook(
    client_factory, seeded_credentials
) -> None:
    tenant, record, raw_key = seeded_credentials
    body = b'{"provider_id":"message-1"}'

    response = client_factory({record.key_hash: record}).post(
        "/inbound-tenant",
        content=body,
        headers={
            **_signed_webhook_headers(raw_key, body),
            "content-type": "application/json",
        },
    )

    assert response.status_code == 200
    assert response.json() == {"tenant_id": str(tenant.id)}


@pytest.mark.parametrize(
    "headers",
    [
        {"X-API-Key": "not-a-valid-api-key"},
        {
            "X-API-Key": "ik_" + "A" * 43,
            "X-Inbound-Timestamp": str(int(time.time())),
            "X-Inbound-Signature": "v1=invalid",
        },
    ],
)
def test_invalid_inbound_webhook_authentication_returns_generic_401(
    client_factory, headers: dict[str, str]
) -> None:
    response = client_factory({}).post(
        "/inbound-tenant",
        content=b"{}",
        headers=headers,
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid inbound webhook"}


def test_active_operator_key_resolves_tenant_actor_and_credential_context(
    client_factory, seeded_credentials
) -> None:
    """Approval authentication returns only the tenant-scoped non-secret actor context."""
    tenant, record, raw_key = seeded_credentials
    record.principal_type = "operator"
    record.capability = "approval_decider"
    record.actor_ref = "ops-alice"

    response = client_factory({record.key_hash: record}).get(
        "/operator", headers={"X-API-Key": raw_key}
    )

    assert response.status_code == 200
    assert response.json() == {
        "tenant_id": str(tenant.id),
        "actor_ref": "ops-alice",
        "credential_id": str(record.id),
    }


def test_service_api_key_cannot_authenticate_as_an_operator(
    client_factory, seeded_credentials
) -> None:
    """Existing service credentials remain valid for tenant APIs but cannot decide approvals."""
    _, record, raw_key = seeded_credentials

    tenant_response = client_factory({record.key_hash: record}).get(
        "/tenant", headers={"X-API-Key": raw_key}
    )
    operator_response = client_factory({record.key_hash: record}).get(
        "/operator", headers={"X-API-Key": raw_key}
    )

    assert tenant_response.status_code == 200
    assert operator_response.status_code == 401


def test_inactive_operator_key_is_rejected(client_factory, seeded_credentials) -> None:
    """An operator credential must remain active at decision time."""
    _, record, raw_key = seeded_credentials
    record.principal_type = "operator"
    record.capability = "approval_decider"
    record.actor_ref = "ops-alice"
    record.is_active = False

    response = client_factory({record.key_hash: record}).get(
        "/operator", headers={"X-API-Key": raw_key}
    )

    assert response.status_code == 401


@pytest.mark.parametrize("header_value", [None, "", "wrong-prefix", "ik_"])
def test_missing_or_malformed_operator_api_key_returns_401(
    client_factory, header_value: str | None
) -> None:
    """Malformed operator credentials are rejected before any tenant lookup."""
    headers = {} if header_value is None else {"X-API-Key": header_value}

    response = client_factory({}).get("/operator", headers=headers)

    assert response.status_code == 401


def test_authentication_responses_and_logs_do_not_disclose_raw_key(
    caplog, client_factory, seeded_credentials
) -> None:
    """The provisioning credential never crosses the authentication response/log boundary."""
    tenant, record, raw_key = seeded_credentials
    caplog.set_level(logging.DEBUG)

    success = client_factory({record.key_hash: record}).get(
        "/tenant", headers={"X-API-Key": raw_key}
    )
    failure = client_factory({}).get("/tenant", headers={"X-API-Key": raw_key})

    assert success.status_code == 200
    assert failure.status_code == 401
    assert raw_key not in success.text
    assert raw_key not in failure.text
    assert raw_key not in caplog.text
    assert str(tenant.id) in success.text
