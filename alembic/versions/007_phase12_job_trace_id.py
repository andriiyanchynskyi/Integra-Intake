"""persist the server-owned observability trace on agent jobs"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "007_phase12_job_trace_id"
down_revision: Union[str, None] = "006_phase8_approval_status_check"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "agent_jobs",
        sa.Column("trace_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.execute("UPDATE agent_jobs SET trace_id = gen_random_uuid() WHERE trace_id IS NULL")
    op.alter_column(
        "agent_jobs",
        "trace_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=False,
    )
    op.create_unique_constraint(
        "uq_agent_jobs_trace_id", "agent_jobs", ["trace_id"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_agent_jobs_trace_id", "agent_jobs", type_="unique")
    op.drop_column("agent_jobs", "trace_id")
