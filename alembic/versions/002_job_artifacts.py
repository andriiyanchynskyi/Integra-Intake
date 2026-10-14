"""Add durable reply-draft and review-flag artifacts."""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "002_job_artifacts"
down_revision: Union[str, None] = "001_initial_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "job_artifacts",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id"),
            nullable=False,
        ),
        sa.Column(
            "job_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("kind", sa.String(50), nullable=False),
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
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "job_id",
            "kind",
            name="uq_job_artifacts_tenant_job_kind",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "job_id"],
            ["agent_jobs.tenant_id", "agent_jobs.id"],
            name="fk_job_artifacts_tenant_job",
        ),
        sa.CheckConstraint(
            "kind IN ('reply_draft', 'review_flag')",
            name="ck_job_artifacts_kind",
        ),
    )
    op.create_index(
        "ix_job_artifacts_tenant_id_id",
        "job_artifacts",
        ["tenant_id", "id"],
    )
    op.create_index(
        "ix_job_artifacts_tenant_id_job_id",
        "job_artifacts",
        ["tenant_id", "job_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_job_artifacts_tenant_id_job_id", table_name="job_artifacts")
    op.drop_index("ix_job_artifacts_tenant_id_id", table_name="job_artifacts")
    op.drop_table("job_artifacts")
