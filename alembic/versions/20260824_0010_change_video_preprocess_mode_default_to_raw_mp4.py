"""change video_preprocess_mode default to raw_mp4

Revision ID: 20260824_0010
Revises: 20260824_0009
Create Date: 2026-08-25 00:00:00.000000

Switches the column-level server_default for
``llm_provider.video_preprocess_mode`` from 'keyframe' to 'raw_mp4'.

With sub-chunk=60s and N=120, the raw_mp4 path sends the 60s mp4
directly to vLLM with ``media_io_kwargs.video.num_frames=120``; vLLM
uniformly samples 120 frames at 2fps under the 100M pixel budget
(1216x672 per frame ≈ 88.7% of 720p). This avoids client-side ffmpeg
decode + MAD/pHash entirely — no cv2/numpy dependency, no keyframe
extraction overhead. Verified against vLLM qwen3.5-9b at
http://192.168.1.102:8000/v1 (prompt_tokens=48,434, 40.9s wall).

The keyframe path remains available for providers that explicitly set
``video_preprocess_mode='keyframe'``.
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "20260824_0010"
down_revision: Union[str, None] = "20260824_0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "llm_provider",
        "video_preprocess_mode",
        server_default="raw_mp4",
        existing_type=sa.String(length=16),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "llm_provider",
        "video_preprocess_mode",
        server_default="keyframe",
        existing_type=sa.String(length=16),
        existing_nullable=False,
    )
