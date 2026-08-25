"""add durable session analysis checkpoints and usage attribution.

Revision ID: 20260826_0015
Revises: 20260825_0014
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "20260826_0015"
down_revision: Union[str, None] = "20260825_0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "session_analysis_checkpoint",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "session_id",
            sa.Integer(),
            sa.ForeignKey("video_session.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("analysis_run_id", sa.String(length=64), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("sub_chunk_index", sa.Integer(), nullable=False),
        sa.Column("start_offset_seconds", sa.Integer(), nullable=False),
        sa.Column("input_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("event_payload", sa.JSON(), nullable=True),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completion_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("error_type", sa.String(length=128), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint(
            "session_id",
            "analysis_run_id",
            "chunk_index",
            "sub_chunk_index",
            name="uq_session_analysis_checkpoint_work",
        ),
    )
    op.create_index(
        "idx_session_analysis_checkpoint_session_run",
        "session_analysis_checkpoint",
        ["session_id", "analysis_run_id"],
    )
    op.create_index(
        "idx_session_analysis_checkpoint_state", "session_analysis_checkpoint", ["state"]
    )
    op.add_column("llm_usage_log", sa.Column("session_id", sa.Integer(), nullable=True))
    op.add_column("llm_usage_log", sa.Column("analysis_checkpoint_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_llm_usage_log_session",
        "llm_usage_log",
        "video_session",
        ["session_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_llm_usage_log_checkpoint",
        "llm_usage_log",
        "session_analysis_checkpoint",
        ["analysis_checkpoint_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("idx_llm_usage_log_session", "llm_usage_log", ["session_id"])
    op.create_unique_constraint(
        "uq_llm_usage_log_checkpoint", "llm_usage_log", ["analysis_checkpoint_id"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_llm_usage_log_checkpoint", "llm_usage_log", type_="unique")
    op.drop_index("idx_llm_usage_log_session", table_name="llm_usage_log")
    op.drop_constraint("fk_llm_usage_log_checkpoint", "llm_usage_log", type_="foreignkey")
    op.drop_constraint("fk_llm_usage_log_session", "llm_usage_log", type_="foreignkey")
    op.drop_column("llm_usage_log", "analysis_checkpoint_id")
    op.drop_column("llm_usage_log", "session_id")
    op.drop_index("idx_session_analysis_checkpoint_state", table_name="session_analysis_checkpoint")
    op.drop_index(
        "idx_session_analysis_checkpoint_session_run", table_name="session_analysis_checkpoint"
    )
    op.drop_table("session_analysis_checkpoint")
