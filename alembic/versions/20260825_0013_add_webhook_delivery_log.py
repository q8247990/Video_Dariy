"""add webhook delivery log

Revision ID: 20260825_0013
Revises: 20260825_0012
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "20260825_0013"
down_revision: Union[str, None] = "20260825_0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "webhook_delivery_log",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("webhook_id", sa.Integer(), sa.ForeignKey("webhook_config.id"), nullable=False),
        sa.Column("event_type", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("error_message", sa.String(length=1024), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("delivered_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_webhook_delivery_log_webhook_id", "webhook_delivery_log", ["webhook_id"])


def downgrade() -> None:
    op.drop_index("ix_webhook_delivery_log_webhook_id", table_name="webhook_delivery_log")
    op.drop_table("webhook_delivery_log")
