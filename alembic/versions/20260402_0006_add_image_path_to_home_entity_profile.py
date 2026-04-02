"""add image_path to home_entity_profile

Revision ID: 20260402_0006
Revises: 20260402_0005
Create Date: 2026-04-02 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "20260402_0006"
down_revision: Union[str, None] = "20260402_0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "home_entity_profile",
        sa.Column("image_path", sa.String(255), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("home_entity_profile", "image_path")
