"""add tenant-scoped human approval workflow

Revision ID: 004_phase8_approval_workflow
Revises: 003_phase7_worker_runtime
Create Date: 2026-09-23
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "004_phase8_approval_workflow"
down_revision: Union[str, None] = "003_phase7_worker_runtime"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "api_keys",
        sa.Column(
            "principal_type",
            sa.String(30),
            server_default=sa.text("'service'"),
            nullable=False,
        ),
    )
    op.add_column(
        "api_keys",
        sa.Column(
            "capability",
            sa.String(50),
            server_default=sa.text("'none'"),
            nullable=False,
        ),
    )
    op.add_column("api_keys", sa.Column("actor_ref", sa.String(255)))

    op.alter_column(
        "approvals",
        "case_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=True,
    )
    op.add_column(
        "approvals",
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column("approvals", sa.Column("policy_reason", sa.String(100)))
    op.add_column("approvals", sa.Column("pending_action", postgresql.JSONB()))
    op.add_column(
        "approvals",
        sa.Column("tenant_config_sha256", sa.String(64)),
    )
    op.add_column(
        "approvals",
        sa.Column("expires_at", sa.DateTime(timezone=True)),
    )
    op.add_column(
        "approvals",
        sa.Column("decided_at", sa.DateTime(timezone=True)),
    )
    op.add_column(
        "approvals",
        sa.Column("decided_by_actor_ref", sa.String(255)),
    )
    op.add_column("approvals", sa.Column("decision_reason", sa.Text()))
    op.add_column(
        "approvals",
        sa.Column("executed_at", sa.DateTime(timezone=True)),
    )
    op.add_column(
        "approvals",
        sa.Column("execution_result", postgresql.JSONB()),
    )

    op.create_unique_constraint(
        "uq_approvals_tenant_id_id", "approvals", ["tenant_id", "id"]
    )
    op.create_unique_constraint("uq_approvals_job_id", "approvals", ["job_id"])
    op.create_foreign_key(
        "fk_approvals_tenant_job",
        "approvals",
        "agent_jobs",
        ["tenant_id", "job_id"],
        ["tenant_id", "id"],
    )
    op.create_check_constraint(
        "ck_approvals_phase8_payload_complete",
        "approvals",
        "job_id IS NULL OR (pending_action IS NOT NULL "
        "AND tenant_config_sha256 IS NOT NULL AND expires_at IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_approvals_status",
        "approvals",
        "status IN ('pending', 'approved', 'rejected', 'expired')",
    )
    op.create_index(
        "ix_approvals_pending_expires_at",
        "approvals",
        ["status", "expires_at"],
    )

    op.create_table(
        "approval_events",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id"),
            nullable=False,
        ),
        sa.Column(
            "approval_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
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
    op.drop_index("ix_approval_events_tenant_id_id", table_name="approval_events")
    op.drop_table("approval_events")
    op.drop_index("ix_approvals_pending_expires_at", table_name="approvals")
    op.drop_constraint(
        "ck_approvals_phase8_payload_complete", "approvals", type_="check"
    )
    op.drop_constraint("ck_approvals_status", "approvals", type_="check")
    op.drop_constraint(
        "fk_approvals_tenant_job", "approvals", type_="foreignkey"
    )
    op.drop_constraint("uq_approvals_job_id", "approvals", type_="unique")
    op.drop_constraint(
        "uq_approvals_tenant_id_id", "approvals", type_="unique"
    )
    for name in (
        "execution_result",
        "executed_at",
        "decision_reason",
        "decided_by_actor_ref",
        "decided_at",
        "expires_at",
        "tenant_config_sha256",
        "pending_action",
        "policy_reason",
        "job_id",
    ):
        op.drop_column("approvals", name)
    op.alter_column(
        "approvals",
        "case_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=False,
    )
    for name in ("actor_ref", "capability", "principal_type"):
        op.drop_column("api_keys", name)
