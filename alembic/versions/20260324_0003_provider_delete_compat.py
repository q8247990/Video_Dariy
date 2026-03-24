"""provider delete compatibility

Revision ID: 20260324_0003
Revises: 20260321_0002
Create Date: 2026-03-24 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260324_0003"
down_revision: Union[str, None] = "20260321_0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "llm_usage_log",
        sa.Column("provider_name_snapshot", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "chat_query_log",
        sa.Column("provider_name_snapshot", sa.Text(), nullable=True),
    )
    op.add_column(
        "daily_summary",
        sa.Column("provider_name_snapshot", sa.String(length=128), nullable=True),
    )

    op.execute(
        """
        UPDATE llm_usage_log AS target
        SET provider_name_snapshot = provider.provider_name
        FROM llm_provider AS provider
        WHERE target.provider_id = provider.id
          AND target.provider_name_snapshot IS NULL
        """
    )
    op.execute(
        """
        UPDATE chat_query_log AS target
        SET provider_name_snapshot = provider.provider_name
        FROM llm_provider AS provider
        WHERE target.provider_id = provider.id
          AND target.provider_name_snapshot IS NULL
        """
    )
    op.execute(
        """
        UPDATE daily_summary AS target
        SET provider_name_snapshot = provider.provider_name
        FROM llm_provider AS provider
        WHERE target.provider_id = provider.id
          AND target.provider_name_snapshot IS NULL
        """
    )

    op.alter_column("llm_usage_log", "provider_id", existing_type=sa.Integer(), nullable=True)

    op.execute(
        "ALTER TABLE chat_query_log DROP CONSTRAINT IF EXISTS chat_query_log_provider_id_fkey"
    )
    op.execute("ALTER TABLE daily_summary DROP CONSTRAINT IF EXISTS daily_summary_provider_id_fkey")
    op.execute("ALTER TABLE llm_usage_log DROP CONSTRAINT IF EXISTS llm_usage_log_provider_id_fkey")

    op.create_foreign_key(
        "chat_query_log_provider_id_fkey",
        "chat_query_log",
        "llm_provider",
        ["provider_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "daily_summary_provider_id_fkey",
        "daily_summary",
        "llm_provider",
        ["provider_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "llm_usage_log_provider_id_fkey",
        "llm_usage_log",
        "llm_provider",
        ["provider_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE chat_query_log DROP CONSTRAINT IF EXISTS chat_query_log_provider_id_fkey"
    )
    op.execute("ALTER TABLE daily_summary DROP CONSTRAINT IF EXISTS daily_summary_provider_id_fkey")
    op.execute("ALTER TABLE llm_usage_log DROP CONSTRAINT IF EXISTS llm_usage_log_provider_id_fkey")

    op.create_foreign_key(
        "chat_query_log_provider_id_fkey",
        "chat_query_log",
        "llm_provider",
        ["provider_id"],
        ["id"],
    )
    op.create_foreign_key(
        "daily_summary_provider_id_fkey",
        "daily_summary",
        "llm_provider",
        ["provider_id"],
        ["id"],
    )
    op.create_foreign_key(
        "llm_usage_log_provider_id_fkey",
        "llm_usage_log",
        "llm_provider",
        ["provider_id"],
        ["id"],
    )

    op.alter_column("llm_usage_log", "provider_id", existing_type=sa.Integer(), nullable=False)

    op.drop_column("daily_summary", "provider_name_snapshot")
    op.drop_column("chat_query_log", "provider_name_snapshot")
    op.drop_column("llm_usage_log", "provider_name_snapshot")
