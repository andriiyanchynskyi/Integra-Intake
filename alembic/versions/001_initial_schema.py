"""Create the IntegraIntake release schema."""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "001_initial_schema"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def uuid_column() -> sa.Column:
    return sa.Column(
        "id",
        postgresql.UUID(as_uuid=True),
        primary_key=True,
        nullable=False,
    )


def timestamps() -> list[sa.Column]:
    return [
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    ]


def upgrade() -> None:
    op.create_table(
        "tenants",
        uuid_column(),
        sa.Column("slug", sa.String(255), nullable=False, unique=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column(
            "status",
            sa.String(50),
            server_default="active",
            nullable=False,
        ),
        *timestamps(),
        sa.CheckConstraint(
            "status IN ('active', 'inactive')",
            name="ck_tenants_status",
        ),
    )

    op.create_table(
        "api_keys",
        uuid_column(),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id"),
            nullable=False,
        ),
        sa.Column("prefix", sa.String(11), nullable=False),
        sa.Column("key_hash", sa.String(255), nullable=False, unique=True),
        sa.Column(
            "is_active",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column(
            "principal_type",
            sa.String(30),
            server_default=sa.text("'service'"),
            nullable=False,
        ),
        sa.Column(
            "capability",
            sa.String(50),
            server_default=sa.text("'none'"),
            nullable=False,
        ),
        sa.Column("actor_ref", sa.String(255)),
        *timestamps(),
        sa.CheckConstraint(
            "principal_type IN ('service', 'operator')",
            name="ck_api_keys_principal_type",
        ),
        sa.CheckConstraint(
            "capability IN ('none', 'approval_decider')",
            name="ck_api_keys_capability",
        ),
    )
    op.create_index("ix_api_keys_tenant_id_id", "api_keys", ["tenant_id", "id"])

    op.create_table(
        "customers",
        uuid_column(),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id"),
            nullable=False,
        ),
        sa.Column("external_id", sa.String(255)),
        sa.Column("name", sa.String(255)),
        sa.Column("email", sa.String(255)),
        sa.Column("phone", sa.String(50)),
        sa.Column(
            "attributes",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        *timestamps(),
        sa.UniqueConstraint("tenant_id", "id", name="uq_customers_tenant_id_id"),
        sa.UniqueConstraint(
            "tenant_id",
            "email",
            name="uq_customers_tenant_id_email",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "external_id",
            name="uq_customers_tenant_id_external_id",
        ),
    )

    op.create_table(
        "intake_cases",
        uuid_column(),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id"),
            nullable=False,
        ),
        sa.Column("customer_id", postgresql.UUID(as_uuid=True)),
        sa.Column(
            "status",
            sa.String(50),
            server_default="received",
            nullable=False,
        ),
        sa.Column("channel", sa.String(100), nullable=False),
        sa.Column("subject", sa.String(500), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("source", sa.String(100)),
        sa.Column(
            "raw_payload",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "extracted_fields",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        *timestamps(),
        sa.UniqueConstraint("tenant_id", "id", name="uq_intake_cases_tenant_id_id"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "customer_id"],
            ["customers.tenant_id", "customers.id"],
            name="fk_intake_cases_tenant_customer",
        ),
        sa.CheckConstraint(
            "status IN ('received')",
            name="ck_intake_cases_status",
        ),
    )

    op.create_table(
        "case_events",
        uuid_column(),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id"),
            nullable=False,
        ),
        sa.Column("case_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column("actor", sa.String(255)),
        sa.Column(
            "payload",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "case_id"],
            ["intake_cases.tenant_id", "intake_cases.id"],
            name="fk_case_events_tenant_case",
        ),
    )
    op.create_index(
        "ix_case_events_tenant_id_id",
        "case_events",
        ["tenant_id", "id"],
    )

    op.create_table(
        "agent_jobs",
        uuid_column(),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id"),
            nullable=False,
        ),
        sa.Column("trace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "status",
            sa.String(50),
            server_default="queued",
            nullable=False,
        ),
        sa.Column("source_snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("tenant_config_snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("tenant_config_sha256", sa.String(64), nullable=False),
        sa.Column(
            "risk_signals",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "attempt_count",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("side_effect_committed_at", sa.DateTime(timezone=True)),
        sa.Column("result", postgresql.JSONB()),
        sa.Column("error_code", sa.String(100)),
        *timestamps(),
        sa.UniqueConstraint("tenant_id", "id", name="uq_agent_jobs_tenant_id_id"),
        sa.UniqueConstraint("trace_id", name="uq_agent_jobs_trace_id"),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'awaiting_approval', "
            "'succeeded', 'failed', 'failed_uncertain')",
            name="ck_agent_jobs_status",
        ),
    )
    op.create_index(
        "ix_agent_jobs_status_available_at",
        "agent_jobs",
        ["status", "available_at", "id"],
    )
    op.create_index(
        "ix_agent_jobs_running_lease",
        "agent_jobs",
        ["status", "lease_expires_at"],
    )

    op.create_table(
        "idempotency_records",
        uuid_column(),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id"),
            nullable=False,
        ),
        sa.Column("key", sa.String(255), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("job_id", postgresql.UUID(as_uuid=True)),
        sa.Column("case_id", postgresql.UUID(as_uuid=True)),
        sa.Column(
            "response",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        *timestamps(),
        sa.UniqueConstraint(
            "tenant_id",
            "key",
            name="uq_idempotency_records_tenant_id_key",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "job_id",
            name="uq_idempotency_records_tenant_id_job_id",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "case_id"],
            ["intake_cases.tenant_id", "intake_cases.id"],
            name="fk_idempotency_records_tenant_case",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "job_id"],
            ["agent_jobs.tenant_id", "agent_jobs.id"],
            name="fk_idempotency_records_tenant_job",
        ),
    )
    op.create_index(
        "ix_idempotency_records_tenant_id_id",
        "idempotency_records",
        ["tenant_id", "id"],
    )

    op.create_table(
        "approvals",
        uuid_column(),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id"),
            nullable=False,
        ),
        sa.Column("case_id", postgresql.UUID(as_uuid=True)),
        sa.Column("job_id", postgresql.UUID(as_uuid=True)),
        sa.Column("action", sa.String(100), nullable=False),
        sa.Column(
            "status",
            sa.String(50),
            server_default="pending",
            nullable=False,
        ),
        sa.Column(
            "decision",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("policy_reason", sa.String(100)),
        sa.Column("pending_action", postgresql.JSONB()),
        sa.Column("tenant_config_sha256", sa.String(64)),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.Column("decided_by_actor_ref", sa.String(255)),
        sa.Column("decision_reason", sa.Text()),
        sa.Column("executed_at", sa.DateTime(timezone=True)),
        sa.Column("execution_result", postgresql.JSONB()),
        *timestamps(),
        sa.UniqueConstraint("tenant_id", "id", name="uq_approvals_tenant_id_id"),
        sa.UniqueConstraint("job_id", name="uq_approvals_job_id"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "case_id"],
            ["intake_cases.tenant_id", "intake_cases.id"],
            name="fk_approvals_tenant_case",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "job_id"],
            ["agent_jobs.tenant_id", "agent_jobs.id"],
            name="fk_approvals_tenant_job",
        ),
        sa.CheckConstraint(
            "job_id IS NULL OR (pending_action IS NOT NULL "
            "AND tenant_config_sha256 IS NOT NULL AND expires_at IS NOT NULL)",
            name="ck_approvals_payload_complete",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'expired')",
            name="ck_approvals_status",
        ),
    )
    op.create_index(
        "ix_approvals_pending_expires_at",
        "approvals",
        ["status", "expires_at", "id"],
    )

    op.create_table(
        "approval_events",
        uuid_column(),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id"),
            nullable=False,
        ),
        sa.Column("approval_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column("actor_ref", sa.String(255)),
        sa.Column(
            "payload",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "approval_id"],
            ["approvals.tenant_id", "approvals.id"],
            name="fk_approval_events_tenant_approval",
        ),
    )
    op.create_index(
        "ix_approval_events_tenant_id_id",
        "approval_events",
        ["tenant_id", "id"],
    )


def downgrade() -> None:
    for table_name in (
        "approval_events",
        "approvals",
        "idempotency_records",
        "agent_jobs",
        "case_events",
        "intake_cases",
        "customers",
        "api_keys",
        "tenants",
    ):
        op.drop_table(table_name)
