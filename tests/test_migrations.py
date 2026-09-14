"""PostgreSQL migration integration tests."""

from uuid import uuid4

import pytest
from sqlalchemy import ForeignKeyConstraint, UniqueConstraint, text
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
TENANT_SCOPED_TABLES = PHASE_TWO_TABLES - {"tenants"}
JSONB_COLUMNS = {
    "customers": {"attributes"},
    "intake_cases": {"raw_payload", "extracted_fields"},
    "case_events": {"payload"},
    "idempotency_records": {"response"},
    "approvals": {"decision"},
}
TIMESTAMPED_TABLES = PHASE_TWO_TABLES - {"case_events"}


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
    assert idempotency_records.c.case_id.nullable is False

    approvals = Base.metadata.tables["approvals"]
    assert approvals.c.action.nullable is False

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
        ("approvals", "intake_cases", ("tenant_id", "case_id")),
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
            "'idempotency_records', 'approvals')"
        )
    )
    assert set(tables.scalars()) == PHASE_TWO_TABLES

    columns = await connection.execute(
        text(
            "SELECT table_name, column_name, data_type, udt_name, column_default, is_nullable "
            "FROM information_schema.columns "
            "WHERE table_schema = 'public'"
        )
    )
    column_map = {
        (row.table_name, row.column_name): row
        for row in columns
    }
    for table_name in PHASE_TWO_TABLES:
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
    assert column_map[("customers", "phone")].is_nullable == "YES"
    assert column_map[("intake_cases", "channel")].is_nullable == "NO"
    assert column_map[("intake_cases", "subject")].is_nullable == "NO"
    assert column_map[("intake_cases", "body")].is_nullable == "NO"
    assert column_map[("case_events", "actor")].is_nullable == "YES"
    assert column_map[("idempotency_records", "request_hash")].is_nullable == "NO"
    assert column_map[("idempotency_records", "case_id")].udt_name == "uuid"
    assert column_map[("approvals", "action")].is_nullable == "NO"

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
    assert {(table_name, "id") for table_name in PHASE_TWO_TABLES}.issubset(
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

    constraints = await connection.execute(
        text(
            "SELECT conname, pg_get_constraintdef(oid) AS definition "
            "FROM pg_constraint WHERE connamespace = 'public'::regnamespace"
        )
    )
    constraint_map = {row.conname: row.definition for row in constraints}
    assert "UNIQUE (tenant_id, key)" in constraint_map["uq_idempotency_records_tenant_id_key"]
    assert any("UNIQUE (slug)" in definition for definition in constraint_map.values())
    for table_name, referenced_table, local_columns in (
        ("intake_cases", "customers", "tenant_id, customer_id"),
        ("case_events", "intake_cases", "tenant_id, case_id"),
        ("idempotency_records", "intake_cases", "tenant_id, case_id"),
        ("approvals", "intake_cases", "tenant_id, case_id"),
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
    """Alembic creates the tenant-domain schema and rejects cross-tenant references."""
    await query_schema_contract(postgres_connection)
    await assert_tenant_boundaries(postgres_connection)
