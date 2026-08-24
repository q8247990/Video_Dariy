"""add video preprocess to llm provider

Revision ID: 20260824_0007
Revises: 20260402_0006
Create Date: 2026-08-24 00:00:00.000000

Adds three columns for the per-provider video preprocessing config
(see ``.omo/plans/2026-08-24-video-keyframe.md`` §2.4 / §4.2):

- ``video_preprocess_mode``: ``"keyframe"`` (default) | ``"raw_mp4"``
- ``video_keyframe_target_n``: default 64
- ``video_keyframe_jpeg_quality``: default 88
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "20260824_0007"
down_revision: Union[str, None] = "20260402_0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "llm_provider",
        sa.Column(
            "video_preprocess_mode",
            sa.String(length=16),
            nullable=False,
            server_default="keyframe",
        ),
    )
    op.add_column(
        "llm_provider",
        sa.Column(
            "video_keyframe_target_n",
            sa.Integer(),
            nullable=False,
            server_default="64",
        ),
    )
    op.add_column(
        "llm_provider",
        sa.Column(
            "video_keyframe_jpeg_quality",
            sa.Integer(),
            nullable=False,
            server_default="88",
        ),
    )


def downgrade() -> None:
    op.drop_column("llm_provider", "video_keyframe_jpeg_quality")
    op.drop_column("llm_provider", "video_keyframe_target_n")
    op.drop_column("llm_provider", "video_preprocess_mode")
