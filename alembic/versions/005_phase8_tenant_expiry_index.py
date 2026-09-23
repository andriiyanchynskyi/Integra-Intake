"""make approval expiry index tenant-first

Revision ID: 005_phase8_tenant_expiry_index
Revises: 004_phase8_approval_workflow
Create Date: 2026-09-23
"""

from typing import Sequence, Union

from alembic import op


revision: str = "005_phase8_tenant_expiry_index"
down_revision: Union[str, None] = "004_phase8_approval_workflow"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_index(
        "ix_approvals_pending_expires_at",
        table_name="approvals",
    )
    op.create_index(
        "ix_approvals_pending_expires_at",
        "approvals",
        ["tenant_id", "status", "expires_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_approvals_pending_expires_at",
        table_name="approvals",
    )
    op.create_index(
        "ix_approvals_pending_expires_at",
        "approvals",
        ["status", "expires_at"],
    )
