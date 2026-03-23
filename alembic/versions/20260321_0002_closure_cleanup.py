"""phase1 closure cleanup

Revision ID: 20260321_0002
Revises: 20260320_0001
Create Date: 2026-03-21 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260321_0002"
down_revision: Union[str, None] = "20260320_0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_index("idx_llm_provider_type_default", table_name="llm_provider")
    op.alter_column("llm_provider", "api_key_encrypted", new_column_name="api_key")
    op.drop_column("llm_provider", "is_default")

    op.drop_column("video_source", "backfill_start_date")

    op.drop_column("video_source_runtime_state", "backfill_stuck_alert_last_notified_at")
    op.drop_column("video_source_runtime_state", "backfill_stuck_alert_active")
    op.drop_column("video_source_runtime_state", "backfill_stuck_alert_counter")
    op.drop_column("video_source_runtime_state", "backfill_last_dispatch_at")
    op.drop_column("video_source_runtime_state", "backfill_cursor_time")
    op.drop_column("video_source_runtime_state", "backfill_paused")


def downgrade() -> None:
    op.add_column(
        "video_source_runtime_state",
        sa.Column(
            "backfill_paused",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "video_source_runtime_state",
        sa.Column("backfill_cursor_time", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "video_source_runtime_state",
        sa.Column("backfill_last_dispatch_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "video_source_runtime_state",
        sa.Column(
            "backfill_stuck_alert_counter",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "video_source_runtime_state",
        sa.Column(
            "backfill_stuck_alert_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "video_source_runtime_state",
        sa.Column("backfill_stuck_alert_last_notified_at", sa.DateTime(), nullable=True),
    )

    op.add_column("video_source", sa.Column("backfill_start_date", sa.Date(), nullable=True))

    op.add_column(
        "llm_provider",
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.alter_column("llm_provider", "api_key", new_column_name="api_key_encrypted")
    op.create_index(
        "idx_llm_provider_type_default",
        "llm_provider",
        ["provider_type", "is_default"],
        unique=False,
    )
