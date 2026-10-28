"""normalize video_preprocess_mode to raw_mp4

Revision ID: 20260825_0011
Revises: 20260824_0010
Create Date: 2026-08-25 12:00:00.000000

The keyframe preprocessing path is disabled as a product decision: the
code (keyframe_extractor / build_chunk_keyframe_payload) remains in the
repository, but it must not be activatable through any configuration
surface (API schema, provider settings UI, or a direct DB write).

This data migration normalizes any lingering ``keyframe`` rows to
``raw_mp4`` so that:

1. the tightened API schema (``raw_mp4`` only) can serialize every
   existing provider row without raising validation errors, and
2. the analyzer runtime (which now force-overrides the column to
   ``raw_mp4`` and only logs a warning) starts from a clean state.

The columns themselves are kept on purpose — only the data is
normalized. ``downgrade()`` refuses to run: restoring the old values
would re-enable exactly the mode this revision removes.
"""

from typing import Sequence

from alembic import op

revision: str = "20260825_0011"
down_revision: str | None = "20260824_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "UPDATE llm_provider SET video_preprocess_mode = 'raw_mp4' "
        "WHERE video_preprocess_mode <> 'raw_mp4'"
    )


def downgrade() -> None:
    raise RuntimeError(
        "video_preprocess_mode normalization is irreversible; restore the "
        "pre-upgrade backup instead of downgrading"
    )
