"""constrain approval lifecycle status

Revision ID: 006_phase8_approval_status_check
Revises: 005_phase8_tenant_expiry_index
Create Date: 2026-09-23
"""

from typing import Sequence, Union

from alembic import op


revision: str = "006_phase8_approval_status_check"
down_revision: Union[str, None] = "005_phase8_tenant_expiry_index"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_check_constraint(
        "ck_approvals_status",
        "approvals",
        "status IN ('pending', 'approved', 'rejected', 'expired')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_approvals_status", "approvals", type_="check")
