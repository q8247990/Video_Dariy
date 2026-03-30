"""add supports_tool_calling to llm_provider

Revision ID: 20260330_0004
Revises: 20260324_0003
Create Date: 2026-03-30 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260330_0004"
down_revision: Union[str, None] = "20260324_0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "llm_provider",
        sa.Column(
            "supports_tool_calling",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("llm_provider", "supports_tool_calling")
