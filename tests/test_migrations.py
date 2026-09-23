"""PostgreSQL migration integration tests."""

from uuid import uuid4

import pytest
from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from app.db.base import Base
import app.db.models  # noqa: F401


PHASE_TWO_TABLES = {
    "tenants",
    "api_keys",
    "customers",
    "intake_cases",
    "case_events",
    "idempotency_records",
    "approvals",
}
PHASE_SEVEN_TABLES = PHASE_TWO_TABLES | {"agent_jobs"}
PHASE_EIGHT_TABLES = PHASE_SEVEN_TABLES | {"approval_events"}
TENANT_SCOPED_TABLES = PHASE_EIGHT_TABLES - {"tenants"}
JSONB_COLUMNS = {
    "customers": {"attributes"},
    "intake_cases": {"raw_payload", "extracted_fields"},
    "case_events": {"payload"},
    "idempotency_records": {"response"},
    "approvals": {"decision", "pending_action", "execution_result"},
    "approval_events": {"payload"},
    "agent_jobs": {
        "source_snapshot",
        "tenant_config_snapshot",
        "risk_signals",
        "result",
    },
}
TIMESTAMPED_TABLES = PHASE_EIGHT_TABLES - {"case_events", "approval_events"}


def test_orm_metadata_declares_tenant_isolation_contract() -> None:
    """ORM metadata preserves the database defaults and tenant-scoped relationships."""
    tenants = Base.metadata.tables["tenants"]
    api_keys = Base.metadata.tables["api_keys"]

    assert tenants.c.slug.nullable is False
    assert tenants.c.status.nullable is False
    assert str(tenants.c.status.server_default.arg) == "active"
    assert any(
        isinstance(constraint, UniqueConstraint)
        and tuple(column.name for column in constraint.columns) == ("slug",)
        for constraint in tenants.constraints
    )
    assert api_keys.c.prefix.nullable is False
    assert api_keys.c.principal_type.nullable is False
    assert str(api_keys.c.principal_type.server_default.arg) == "service"
    assert api_keys.c.capability.nullable is False
    assert str(api_keys.c.capability.server_default.arg) == "none"
    assert api_keys.c.actor_ref.nullable is True

    customers = Base.metadata.tables["customers"]
    assert customers.c.phone.nullable is True

    intake_cases = Base.metadata.tables["intake_cases"]
    assert str(intake_cases.c.status.server_default.arg) == "received"
    assert intake_cases.c.channel.nullable is False
    assert intake_cases.c.subject.nullable is False
    assert intake_cases.c.body.nullable is False

    case_events = Base.metadata.tables["case_events"]
    assert case_events.c.actor.nullable is True

    idempotency_records = Base.metadata.tables["idempotency_records"]
    assert idempotency_records.c.request_hash.nullable is False
    assert idempotency_records.c.case_id.nullable is True
    assert idempotency_records.c.job_id.nullable is True

    agent_jobs = Base.metadata.tables["agent_jobs"]
    assert isinstance(agent_jobs.c.id.type, PG_UUID)
    assert isinstance(agent_jobs.c.tenant_id.type, PG_UUID)
    assert agent_jobs.c.tenant_id.nullable is False
    assert agent_jobs.c.status.nullable is False
    assert str(agent_jobs.c.status.server_default.arg) == "queued"
    assert agent_jobs.c.attempt_count.nullable is False
    assert str(agent_jobs.c.attempt_count.server_default.arg) == "0"
    assert agent_jobs.c.tenant_config_sha256.nullable is False
    for name in (
        "source_snapshot",
        "tenant_config_snapshot",
        "risk_signals",
    ):
        assert isinstance(agent_jobs.c[name].type, JSONB)
        assert agent_jobs.c[name].nullable is False
    assert agent_jobs.c.result.nullable is True
    assert isinstance(agent_jobs.c.result.type, JSONB)
    assert agent_jobs.c.available_at.nullable is False
    for name in (
        "lease_expires_at",
        "started_at",
        "finished_at",
        "side_effect_committed_at",
        "error_code",
    ):
        assert agent_jobs.c[name].nullable is True
    assert agent_jobs.c.created_at.nullable is False
    assert agent_jobs.c.updated_at.nullable is False

    assert any(
        isinstance(constraint, UniqueConstraint)
        and constraint.name == "uq_agent_jobs_tenant_id_id"
        and tuple(column.name for column in constraint.columns) == ("tenant_id", "id")
        for constraint in agent_jobs.constraints
    )
    assert {
        index.name: tuple(column.name for column in index.columns)
        for index in agent_jobs.indexes
    } == {
        "ix_agent_jobs_tenant_id_id": ("tenant_id", "id"),
        "ix_agent_jobs_status_available_at": ("status", "available_at"),
        "ix_agent_jobs_running_lease": ("status", "lease_expires_at"),
    }

    approvals = Base.metadata.tables["approvals"]
    assert approvals.c.action.nullable is False
    for name in (
        "case_id",
        "job_id",
        "policy_reason",
        "pending_action",
        "tenant_config_sha256",
        "expires_at",
        "decided_at",
        "decided_by_actor_ref",
        "decision_reason",
        "executed_at",
        "execution_result",
    ):
        assert approvals.c[name].nullable is True
    for name in ("pending_action", "execution_result"):
        assert isinstance(approvals.c[name].type, JSONB)
    assert any(
        isinstance(constraint, UniqueConstraint)
        and constraint.name == "uq_approvals_tenant_id_id"
        and tuple(column.name for column in constraint.columns) == ("tenant_id", "id")
        for constraint in approvals.constraints
    )
    assert any(
        isinstance(constraint, UniqueConstraint)
        and constraint.name == "uq_approvals_job_id"
        and tuple(column.name for column in constraint.columns) == ("job_id",)
        for constraint in approvals.constraints
    )
    assert any(
        isinstance(constraint, CheckConstraint)
        and constraint.name == "ck_approvals_phase8_payload_complete"
        for constraint in approvals.constraints
    )
    assert {
        index.name: tuple(column.name for column in index.columns)
        for index in approvals.indexes
    }["ix_approvals_pending_expires_at"] == (
        "tenant_id",
        "status",
        "expires_at",
    )

    approval_events = Base.metadata.tables["approval_events"]
    assert approval_events.c.tenant_id.nullable is False
    assert approval_events.c.approval_id.nullable is False
    assert approval_events.c.event_type.nullable is False
    assert approval_events.c.actor_ref.nullable is True
    assert approval_events.c.payload.nullable is False
    assert isinstance(approval_events.c.payload.type, JSONB)
    assert approval_events.c.created_at.nullable is False

    for table_name in ("customers", "intake_cases"):
        assert any(
            isinstance(constraint, UniqueConstraint)
            and tuple(column.name for column in constraint.columns) == ("tenant_id", "id")
            for constraint in Base.metadata.tables[table_name].constraints
        )

    for table_name, referenced_table, local_columns in (
        ("intake_cases", "customers", ("tenant_id", "customer_id")),
        ("case_events", "intake_cases", ("tenant_id", "case_id")),
        ("idempotency_records", "intake_cases", ("tenant_id", "case_id")),
        ("idempotency_records", "agent_jobs", ("tenant_id", "job_id")),
        ("approvals", "intake_cases", ("tenant_id", "case_id")),
        ("approvals", "agent_jobs", ("tenant_id", "job_id")),
        ("approval_events", "approvals", ("tenant_id", "approval_id")),
    ):
        assert any(
            isinstance(constraint, ForeignKeyConstraint)
            and tuple(column.name for column in constraint.columns) == local_columns
            and tuple(element.target_fullname for element in constraint.elements)
            == (f"{referenced_table}.tenant_id", f"{referenced_table}.id")
            for constraint in Base.metadata.tables[table_name].constraints
        )


async def query_schema_contract(connection: AsyncConnection) -> None:
    tables = await connection.execute(
        text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' "
            "AND table_name IN "
            "('tenants', 'api_keys', 'customers', 'intake_cases', 'case_events', "
            "'idempotency_records', 'approvals', 'agent_jobs', 'approval_events')"
        )
    )
    assert set(tables.scalars()) == PHASE_EIGHT_TABLES

    columns = await connection.execute(
        text(
            "SELECT table_name, column_name, data_type, udt_name, column_default, "
            "character_maximum_length, is_nullable "
            "FROM information_schema.columns "
            "WHERE table_schema = 'public'"
        )
    )
    column_map = {
        (row.table_name, row.column_name): row
        for row in columns
    }
    for table_name in PHASE_EIGHT_TABLES:
        assert column_map[(table_name, "id")].udt_name == "uuid"
    for table_name in TENANT_SCOPED_TABLES:
        assert column_map[(table_name, "tenant_id")].udt_name == "uuid"
        assert (table_name, "created_at") in column_map
    for table_name in TIMESTAMPED_TABLES:
        assert (table_name, "updated_at") in column_map
    for table_name, names in JSONB_COLUMNS.items():
        for name in names:
            assert column_map[(table_name, name)].udt_name == "jsonb"
    assert "received" in column_map[("intake_cases", "status")].column_default
    assert column_map[("tenants", "slug")].is_nullable == "NO"
    assert "active" in column_map[("tenants", "status")].column_default
    assert column_map[("api_keys", "prefix")].is_nullable == "NO"
    assert column_map[("api_keys", "principal_type")].is_nullable == "NO"
    assert "service" in column_map[("api_keys", "principal_type")].column_default
    assert column_map[("api_keys", "capability")].is_nullable == "NO"
    assert "none" in column_map[("api_keys", "capability")].column_default
    assert column_map[("api_keys", "actor_ref")].is_nullable == "YES"
    assert column_map[("customers", "phone")].is_nullable == "YES"
    assert column_map[("intake_cases", "channel")].is_nullable == "NO"
    assert column_map[("intake_cases", "subject")].is_nullable == "NO"
    assert column_map[("intake_cases", "body")].is_nullable == "NO"
    assert column_map[("case_events", "actor")].is_nullable == "YES"
    assert column_map[("idempotency_records", "request_hash")].is_nullable == "NO"
    assert column_map[("idempotency_records", "case_id")].udt_name == "uuid"
    assert column_map[("idempotency_records", "case_id")].is_nullable == "YES"
    assert column_map[("idempotency_records", "job_id")].udt_name == "uuid"
    assert column_map[("idempotency_records", "job_id")].is_nullable == "YES"
    assert column_map[("approvals", "action")].is_nullable == "NO"
    for name in (
        "case_id",
        "job_id",
        "policy_reason",
        "pending_action",
        "tenant_config_sha256",
        "expires_at",
        "decided_at",
        "decided_by_actor_ref",
        "decision_reason",
        "executed_at",
        "execution_result",
    ):
        assert column_map[("approvals", name)].is_nullable == "YES"
    assert column_map[("approvals", "case_id")].udt_name == "uuid"
    assert column_map[("approvals", "job_id")].udt_name == "uuid"
    assert column_map[("approvals", "tenant_config_sha256")].character_maximum_length == 64
    assert column_map[("approval_events", "tenant_id")].is_nullable == "NO"
    assert column_map[("approval_events", "approval_id")].udt_name == "uuid"
    assert column_map[("approval_events", "event_type")].is_nullable == "NO"
    assert column_map[("approval_events", "actor_ref")].is_nullable == "YES"
    assert column_map[("approval_events", "payload")].is_nullable == "NO"
    assert column_map[("agent_jobs", "tenant_config_sha256")].character_maximum_length == 64
    assert column_map[("agent_jobs", "source_snapshot")].is_nullable == "NO"
    assert column_map[("agent_jobs", "tenant_config_snapshot")].is_nullable == "NO"
    assert column_map[("agent_jobs", "risk_signals")].is_nullable == "NO"
    assert column_map[("agent_jobs", "result")].is_nullable == "YES"
    assert column_map[("agent_jobs", "status")].is_nullable == "NO"
    assert column_map[("agent_jobs", "attempt_count")].is_nullable == "NO"
    assert "queued" in column_map[("agent_jobs", "status")].column_default
    assert "0" in column_map[("agent_jobs", "attempt_count")].column_default
    for name in (
        "available_at",
        "lease_expires_at",
        "started_at",
        "finished_at",
        "side_effect_committed_at",
        "error_code",
        "created_at",
        "updated_at",
    ):
        assert ("agent_jobs", name) in column_map

    primary_keys = await connection.execute(
        text(
            "SELECT tc.table_name, kcu.column_name "
            "FROM information_schema.table_constraints AS tc "
            "JOIN information_schema.key_column_usage AS kcu "
            "ON tc.constraint_name = kcu.constraint_name "
            "AND tc.table_schema = kcu.table_schema "
            "WHERE tc.table_schema = 'public' AND tc.constraint_type = 'PRIMARY KEY'"
        )
    )
    primary_key_columns = {(row.table_name, row.column_name) for row in primary_keys}
    assert {(table_name, "id") for table_name in PHASE_EIGHT_TABLES}.issubset(
        primary_key_columns
    )

    indexes = await connection.execute(
        text(
            "SELECT tablename, indexname, indexdef FROM pg_indexes "
            "WHERE schemaname = 'public'"
        )
    )
    index_map = {(row.tablename, row.indexname): row.indexdef for row in indexes}
    for table_name in TENANT_SCOPED_TABLES:
        assert f"(tenant_id, id)" in index_map[(table_name, f"ix_{table_name}_tenant_id_id")]
    assert "(tenant_id, status, expires_at)" in index_map[
        ("approvals", "ix_approvals_pending_expires_at")
    ]

    constraints = await connection.execute(
        text(
            "SELECT conname, pg_get_constraintdef(oid) AS definition "
            "FROM pg_constraint WHERE connamespace = 'public'::regnamespace"
        )
    )
    constraint_map = {row.conname: row.definition for row in constraints}
    assert "UNIQUE (tenant_id, key)" in constraint_map["uq_idempotency_records_tenant_id_key"]
    assert "UNIQUE (tenant_id, id)" in constraint_map["uq_approvals_tenant_id_id"]
    assert "UNIQUE (job_id)" in constraint_map["uq_approvals_job_id"]
    assert any("UNIQUE (slug)" in definition for definition in constraint_map.values())
    for table_name, referenced_table, local_columns in (
        ("intake_cases", "customers", "tenant_id, customer_id"),
        ("case_events", "intake_cases", "tenant_id, case_id"),
        ("idempotency_records", "intake_cases", "tenant_id, case_id"),
        ("idempotency_records", "agent_jobs", "tenant_id, job_id"),
        ("approvals", "intake_cases", "tenant_id, case_id"),
        ("approvals", "agent_jobs", "tenant_id, job_id"),
        ("approval_events", "approvals", "tenant_id, approval_id"),
    ):
        assert any(
            f"FOREIGN KEY ({local_columns}) REFERENCES {referenced_table}(tenant_id, id)" in definition
            for definition in constraint_map.values()
        )


async def assert_integrity_error(connection: AsyncConnection, statement: str, parameters: dict[str, object]) -> None:
    savepoint = await connection.begin_nested()
    try:
        with pytest.raises(IntegrityError):
            await connection.execute(text(statement), parameters)
    finally:
        await savepoint.rollback()


async def assert_tenant_boundaries(connection: AsyncConnection) -> None:
    tenant_a, tenant_b = uuid4(), uuid4()
    customer_b, case_b = uuid4(), uuid4()
    await connection.execute(
        text("INSERT INTO tenants (id, slug, name) VALUES (:id, :slug, :name)"),
        [
            {"id": tenant_a, "slug": "tenant-a", "name": "Tenant A"},
            {"id": tenant_b, "slug": "tenant-b", "name": "Tenant B"},
        ],
    )
    await connection.execute(
        text("INSERT INTO customers (id, tenant_id) VALUES (:id, :tenant_id)"),
        {"id": customer_b, "tenant_id": tenant_b},
    )
    await connection.execute(
        text(
            "INSERT INTO intake_cases (id, tenant_id, channel, subject, body) "
            "VALUES (:id, :tenant_id, :channel, :subject, :body)"
        ),
        {
            "id": case_b,
            "tenant_id": tenant_b,
            "channel": "email",
            "subject": "Case B",
            "body": "Body B",
        },
    )
    agent_job_a, agent_job_b = uuid4(), uuid4()
    await connection.execute(
        text(
            "INSERT INTO agent_jobs "
            "(id, tenant_id, source_snapshot, tenant_config_snapshot, tenant_config_sha256) "
            "VALUES (:id, :tenant_id, '{}'::jsonb, '{}'::jsonb, :sha256)"
        ),
        [
            {"id": agent_job_a, "tenant_id": tenant_a, "sha256": "a" * 64},
            {"id": agent_job_b, "tenant_id": tenant_b, "sha256": "b" * 64},
        ],
    )
    legacy_approval_id = uuid4()
    await connection.execute(
        text(
            "INSERT INTO approvals (id, tenant_id, case_id, action) "
            "VALUES (:id, :tenant_id, :case_id, :action)"
        ),
        {
            "id": legacy_approval_id,
            "tenant_id": tenant_b,
            "case_id": case_b,
            "action": "legacy_action",
        },
    )
    legacy_approval = await connection.execute(
        text(
            "SELECT job_id, policy_reason, pending_action, tenant_config_sha256, "
            "expires_at, decided_at, decided_by_actor_ref, decision_reason, "
            "executed_at, execution_result "
            "FROM approvals WHERE id = :id AND tenant_id = :tenant_id"
        ),
        {"id": legacy_approval_id, "tenant_id": tenant_b},
    )
    legacy_row = legacy_approval.one()
    assert all(value is None for value in legacy_row)
    await assert_integrity_error(
        connection,
        "INSERT INTO intake_cases (id, tenant_id, customer_id) VALUES (:id, :tenant_id, :customer_id)",
        {"id": uuid4(), "tenant_id": tenant_a, "customer_id": customer_b},
    )
    await assert_integrity_error(
        connection,
        "INSERT INTO case_events (id, tenant_id, case_id, event_type) VALUES (:id, :tenant_id, :case_id, :event_type)",
        {"id": uuid4(), "tenant_id": tenant_a, "case_id": case_b, "event_type": "received"},
    )
    await assert_integrity_error(
        connection,
        "INSERT INTO approvals (id, tenant_id, case_id, action) VALUES (:id, :tenant_id, :case_id, :action)",
        {"id": uuid4(), "tenant_id": tenant_a, "case_id": case_b, "action": "send_reply"},
    )
    await assert_integrity_error(
        connection,
        "INSERT INTO idempotency_records (id, tenant_id, key, request_hash, case_id) "
        "VALUES (:id, :tenant_id, :key, :request_hash, :case_id)",
        {
            "id": uuid4(),
            "tenant_id": tenant_a,
            "key": "cross-tenant-key",
            "request_hash": "a" * 64,
            "case_id": case_b,
        },
    )
    await assert_integrity_error(
        connection,
        "INSERT INTO approvals (id, tenant_id, job_id, action, pending_action, "
        "tenant_config_sha256, expires_at) "
        "VALUES (:id, :tenant_id, :job_id, :action, '{}'::jsonb, :sha256, "
        "now() + interval '1 hour')",
        {
            "id": uuid4(),
            "tenant_id": tenant_a,
            "job_id": agent_job_b,
            "action": "create_case",
            "sha256": "b" * 64,
        },
    )
    approval_b_id = uuid4()
    await connection.execute(
        text(
            "INSERT INTO approvals (id, tenant_id, job_id, action, pending_action, "
            "tenant_config_sha256, expires_at) "
            "VALUES (:id, :tenant_id, :job_id, :action, '{}'::jsonb, :sha256, "
            "now() + interval '1 hour')"
        ),
        {
            "id": approval_b_id,
            "tenant_id": tenant_b,
            "job_id": agent_job_b,
            "action": "create_case",
            "sha256": "b" * 64,
        },
    )
    await assert_integrity_error(
        connection,
        "INSERT INTO approvals (id, tenant_id, job_id, action, pending_action, "
        "tenant_config_sha256, expires_at) "
        "VALUES (:id, :tenant_id, :job_id, :action, '{}'::jsonb, :sha256, "
        "now() + interval '1 hour')",
        {
            "id": uuid4(),
            "tenant_id": tenant_b,
            "job_id": agent_job_b,
            "action": "create_case",
            "sha256": "b" * 64,
        },
    )
    await assert_integrity_error(
        connection,
        "INSERT INTO approval_events "
        "(id, tenant_id, approval_id, event_type) "
        "VALUES (:id, :tenant_id, :approval_id, :event_type)",
        {
            "id": uuid4(),
            "tenant_id": tenant_a,
            "approval_id": approval_b_id,
            "event_type": "approval_requested",
        },
    )
    await assert_integrity_error(
        connection,
        "INSERT INTO idempotency_records (id, tenant_id, key, request_hash, job_id) "
        "VALUES (:id, :tenant_id, :key, :request_hash, :job_id)",
        {
            "id": uuid4(),
            "tenant_id": tenant_a,
            "key": "cross-tenant-job-key",
            "request_hash": "d" * 64,
            "job_id": agent_job_b,
        },
    )
    await connection.execute(
        text(
            "INSERT INTO idempotency_records (id, tenant_id, key, request_hash, job_id) "
            "VALUES (:id, :tenant_id, :key, :request_hash, :job_id)"
        ),
        {
            "id": uuid4(),
            "tenant_id": tenant_b,
            "key": "job-only-key",
            "request_hash": "e" * 64,
            "job_id": agent_job_b,
        },
    )
    record_id = uuid4()
    await connection.execute(
        text(
            "INSERT INTO idempotency_records (id, tenant_id, key, request_hash, case_id) "
            "VALUES (:id, :tenant_id, :key, :request_hash, :case_id)"
        ),
        {
            "id": record_id,
            "tenant_id": tenant_b,
            "key": "duplicate-key",
            "request_hash": "b" * 64,
            "case_id": case_b,
        },
    )
    await assert_integrity_error(
        connection,
        "INSERT INTO idempotency_records (id, tenant_id, key, request_hash, case_id) "
        "VALUES (:id, :tenant_id, :key, :request_hash, :case_id)",
        {
            "id": uuid4(),
            "tenant_id": tenant_b,
            "key": "duplicate-key",
            "request_hash": "c" * 64,
            "case_id": case_b,
        },
    )


async def test_phase_two_migration_creates_tenant_domain_schema(
    postgres_connection: AsyncConnection,
) -> None:
    """Alembic creates the worker schema and rejects cross-tenant references."""
    await query_schema_contract(postgres_connection)
    await assert_tenant_boundaries(postgres_connection)
