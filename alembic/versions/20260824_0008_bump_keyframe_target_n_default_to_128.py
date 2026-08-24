"""bump video_keyframe_target_n default to 128

Revision ID: 20260824_0008
Revises: 20260824_0007
Create Date: 2026-08-24 00:00:00.000000

Raises the column-level server_default for
``llm_provider.video_keyframe_target_n`` from 64 to 128, aligning the
default with REPORT §6 (10-min chunk → N=128 guidance). Existing rows
keep whatever value they already have; only new inserts fall back to
128 when the client omits the field.

Companion code change: src/schemas/llm_provider.py LLMProviderBase
default also bumped to 128 so Pydantic and the DB default agree.
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "20260824_0008"
down_revision: Union[str, None] = "20260824_0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "llm_provider",
        "video_keyframe_target_n",
        server_default="128",
        existing_type=sa.Integer(),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "llm_provider",
        "video_keyframe_target_n",
        server_default="64",
        existing_type=sa.Integer(),
        existing_nullable=False,
    )
