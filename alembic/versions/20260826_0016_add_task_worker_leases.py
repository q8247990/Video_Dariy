"""add durable worker lease metadata to task logs.

Revision ID: 20260826_0016
Revises: 20260826_0015
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "20260826_0016"
down_revision: Union[str, None] = "20260826_0015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "task_log", sa.Column("recovery_attempt", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column("task_log", sa.Column("lease_owner", sa.String(length=128), nullable=True))
    op.add_column(
        "task_log", sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "task_log", sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_index("idx_task_log_lease_expires_at", "task_log", ["lease_expires_at"])


def downgrade() -> None:
    op.drop_index("idx_task_log_lease_expires_at", table_name="task_log")
    op.drop_column("task_log", "lease_expires_at")
    op.drop_column("task_log", "last_heartbeat_at")
    op.drop_column("task_log", "lease_owner")
    op.drop_column("task_log", "recovery_attempt")
