"""worker runtime jobs and pre-case idempotency linkage

Revision ID: 003_phase7_worker_runtime
Revises: 002_domain_and_tenancy
Create Date: 2026-09-21
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "003_phase7_worker_runtime"
down_revision: Union[str, None] = "002_domain_and_tenancy"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "agent_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id"),
            nullable=False,
        ),
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
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
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
        sa.UniqueConstraint("tenant_id", "id", name="uq_agent_jobs_tenant_id_id"),
    )
    op.create_index(
        "ix_agent_jobs_tenant_id_id", "agent_jobs", ["tenant_id", "id"]
    )
    op.create_index(
        "ix_agent_jobs_status_available_at",
        "agent_jobs",
        ["status", "available_at"],
    )
    op.create_index(
        "ix_agent_jobs_running_lease",
        "agent_jobs",
        ["status", "lease_expires_at"],
    )

    op.add_column(
        "idempotency_records",
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.drop_constraint(
        "fk_idempotency_records_tenant_case",
        "idempotency_records",
        type_="foreignkey",
    )
    op.alter_column(
        "idempotency_records",
        "case_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=True,
    )
    op.create_foreign_key(
        "fk_idempotency_records_tenant_case",
        "idempotency_records",
        "intake_cases",
        ["tenant_id", "case_id"],
        ["tenant_id", "id"],
    )
    op.create_foreign_key(
        "fk_idempotency_records_tenant_job",
        "idempotency_records",
        "agent_jobs",
        ["tenant_id", "job_id"],
        ["tenant_id", "id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_idempotency_records_tenant_job",
        "idempotency_records",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_idempotency_records_tenant_case",
        "idempotency_records",
        type_="foreignkey",
    )
    op.alter_column(
        "idempotency_records",
        "case_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=False,
    )
    op.create_foreign_key(
        "fk_idempotency_records_tenant_case",
        "idempotency_records",
        "intake_cases",
        ["tenant_id", "case_id"],
        ["tenant_id", "id"],
    )
    op.drop_column("idempotency_records", "job_id")

    op.drop_index("ix_agent_jobs_running_lease", table_name="agent_jobs")
    op.drop_index("ix_agent_jobs_status_available_at", table_name="agent_jobs")
    op.drop_index("ix_agent_jobs_tenant_id_id", table_name="agent_jobs")
    op.drop_table("agent_jobs")
