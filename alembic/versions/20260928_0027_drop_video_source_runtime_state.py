"""drop the unused video_source_runtime_state table

Revision ID: 20260928_0027
Revises: 20260928_0026
Create Date: 2026-09-28 00:00:00.000000

Why
---
``video_source_runtime_state`` only ever carried the video-latency alert
feature (``latency_alert_counter`` / ``latency_alert_active`` /
``latency_alert_last_notified_at``). That feature was never wired up: no
service read or wrote the table, and the matching system-config keys /
``TaskType.VIDEO_PIPELINE_ALERT`` were likewise unused. The feature has
been removed from the codebase, so the table is dropped here.

Reversibility
-------------
``downgrade()`` recreates the empty table with the same seven-column
shape (id / source_id / three latency-alert columns / created_at /
updated_at) and its unique index. No row data is preserved because the
table was never populated.
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "20260928_0027"
down_revision: Union[str, None] = "20260928_0026"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "video_source_runtime_state"
UNIQUE_INDEX = "idx_video_source_runtime_state_source"


def upgrade() -> None:
    bind = op.get_bind()
    exists = bind.execute(
        sa.text(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = current_schema() AND table_name = :table"
        ),
        {"table": TABLE},
    ).fetchone()
    if exists is None:
        raise RuntimeError(
            f"{TABLE} is missing from the current schema; the database is already "
            "past this revision or was changed out-of-band. Investigate before stamping."
        )
    op.drop_table(TABLE)


def downgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "source_id",
            sa.Integer(),
            sa.ForeignKey("video_source.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "latency_alert_counter", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("latency_alert_active", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("latency_alert_last_notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(UNIQUE_INDEX, TABLE, ["source_id"], unique=True)
