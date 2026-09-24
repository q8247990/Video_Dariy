"""replace importance_level with rule-based attention.

Revision ID: 20260923_0025
Revises: 20260923_0024
Create Date: 2026-09-23 13:00:00.000000

The model no longer judges importance; attention is derived by rule
(see :mod:`src.services.attention`). This migration removes the
subjective ``event_record.importance_level`` column and renames
``video_session.has_important_event`` to ``has_attention_event`` so the
session-level flag matches the new semantics.

Destructive / irreversible
--------------------------

The dropped ``importance_level`` values cannot be reconstructed from the
schema alone. Per AGENTS §10, run a verified database backup before
applying. ``downgrade()`` recreates an empty column; restore from the
pre-upgrade backup if the historical importance values must be kept.
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "20260923_0025"
down_revision: Union[str, None] = "20260923_0024"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_index("ix_event_record_importance_level", table_name="event_record")
    op.drop_column("event_record", "importance_level")
    op.alter_column("video_session", "has_important_event", new_column_name="has_attention_event")


def downgrade() -> None:
    op.alter_column("video_session", "has_attention_event", new_column_name="has_important_event")
    op.add_column(
        "event_record", sa.Column("importance_level", sa.String(length=16), nullable=True)
    )
    op.create_index("ix_event_record_importance_level", "event_record", ["importance_level"])
