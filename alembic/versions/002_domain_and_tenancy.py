"""domain and tenancy

Revision ID: 002_domain_and_tenancy
Revises: 001_bootstrap
Create Date: 2026-09-10
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "002_domain_and_tenancy"
down_revision: Union[str, None] = "001_bootstrap"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def uuid_column() -> sa.Column:
    return sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False)


def timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "tenants",
        uuid_column(),
        sa.Column("slug", sa.String(255), nullable=False, unique=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("status", sa.String(50), server_default="active", nullable=False),
        *timestamps(),
    )
    op.create_table(
        "api_keys", uuid_column(), sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("prefix", sa.String(11), nullable=False),
        sa.Column("key_hash", sa.String(255), nullable=False, unique=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False), *timestamps(),
    )
    op.create_index("ix_api_keys_tenant_id_id", "api_keys", ["tenant_id", "id"])
    op.create_table(
        "customers", uuid_column(), sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("external_id", sa.String(255)), sa.Column("name", sa.String(255)), sa.Column("email", sa.String(255)),
        sa.Column("phone", sa.String(50)),
        sa.Column("attributes", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False), *timestamps(),
        sa.UniqueConstraint("tenant_id", "id", name="uq_customers_tenant_id_id"),
    )
    op.create_index("ix_customers_tenant_id_id", "customers", ["tenant_id", "id"])
    op.create_table(
        "intake_cases", uuid_column(), sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("customer_id", postgresql.UUID(as_uuid=True)),
        sa.Column("status", sa.String(50), server_default="received", nullable=False),
        sa.Column("channel", sa.String(100), nullable=False), sa.Column("subject", sa.String(500), nullable=False),
        sa.Column("body", sa.Text(), nullable=False), sa.Column("source", sa.String(100)),
        sa.Column("raw_payload", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("extracted_fields", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False), *timestamps(),
        sa.UniqueConstraint("tenant_id", "id", name="uq_intake_cases_tenant_id_id"),
        sa.ForeignKeyConstraint(["tenant_id", "customer_id"], ["customers.tenant_id", "customers.id"], name="fk_intake_cases_tenant_customer"),
    )
    op.create_index("ix_intake_cases_tenant_id_id", "intake_cases", ["tenant_id", "id"])
    op.create_table(
        "case_events", uuid_column(), sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("case_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column("actor", sa.String(255), nullable=True),
        sa.Column("payload", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id", "case_id"], ["intake_cases.tenant_id", "intake_cases.id"], name="fk_case_events_tenant_case"),
    )
    op.create_index("ix_case_events_tenant_id_id", "case_events", ["tenant_id", "id"])
    op.create_table(
        "idempotency_records", uuid_column(), sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("key", sa.String(255), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False), sa.Column("case_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("response", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False), *timestamps(),
        sa.UniqueConstraint("tenant_id", "key", name="uq_idempotency_records_tenant_id_key"),
        sa.ForeignKeyConstraint(["tenant_id", "case_id"], ["intake_cases.tenant_id", "intake_cases.id"], name="fk_idempotency_records_tenant_case"),
    )
    op.create_index("ix_idempotency_records_tenant_id_id", "idempotency_records", ["tenant_id", "id"])
    op.create_table(
        "approvals", uuid_column(), sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("case_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("action", sa.String(100), nullable=False),
        sa.Column("status", sa.String(50), server_default="pending", nullable=False),
        sa.Column("decision", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False), *timestamps(),
        sa.ForeignKeyConstraint(["tenant_id", "case_id"], ["intake_cases.tenant_id", "intake_cases.id"], name="fk_approvals_tenant_case"),
    )
    op.create_index("ix_approvals_tenant_id_id", "approvals", ["tenant_id", "id"])


def downgrade() -> None:
    for table in ("approvals", "idempotency_records", "case_events", "intake_cases", "customers", "api_keys"):
        op.drop_index(f"ix_{table}_tenant_id_id", table_name=table)
        op.drop_table(table)
    op.drop_table("tenants")
