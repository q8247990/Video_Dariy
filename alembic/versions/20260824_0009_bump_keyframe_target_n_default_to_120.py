"""bump video_keyframe_target_n default to 120

Revision ID: 20260824_0009
Revises: 20260824_0008
Create Date: 2026-08-25 00:00:00.000000

Raises the column-level server_default for
``llm_provider.video_keyframe_target_n`` from 128 to 120, aligning with
the 1-min sub-chunk design (60s × 2fps = 120 frames, 100M budget →
1216×672 per frame ≈ 88.7% of 720p).
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "20260824_0009"
down_revision: Union[str, None] = "20260824_0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "llm_provider",
        "video_keyframe_target_n",
        server_default="120",
        existing_type=sa.Integer(),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "llm_provider",
        "video_keyframe_target_n",
        server_default="128",
        existing_type=sa.Integer(),
        existing_nullable=False,
    )
