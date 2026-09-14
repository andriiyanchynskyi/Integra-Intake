"""bootstrap

Revision ID: 001_bootstrap
Revises:
Create Date: 2026-03-08

"""

from typing import Sequence, Union

# revision identifiers, used by Alembic.
revision: str = "001_bootstrap"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
