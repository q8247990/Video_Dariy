"""add missing indexes

Revision ID: 20260402_0005
Revises: 20260330_0004
Create Date: 2026-04-02 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op

revision: str = "20260402_0005"
down_revision: Union[str, None] = "20260330_0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index("idx_webhook_config_enabled", "webhook_config", ["enabled"])
    op.create_index("idx_event_tag_rel_event", "event_tag_rel", ["event_id"])
    op.create_index(
        "idx_video_session_file_rel_sort",
        "video_session_file_rel",
        ["session_id", "sort_index"],
    )
    op.create_index(
        "idx_home_entity_profile_type_enabled",
        "home_entity_profile",
        ["entity_type", "is_enabled"],
    )
    op.create_index(
        "idx_tag_definition_type_enabled",
        "tag_definition",
        ["tag_type", "enabled"],
    )
    op.create_index(
        "idx_daily_summary_generated_at",
        "daily_summary",
        ["generated_at"],
    )
    op.create_index(
        "idx_video_source_enabled_paused",
        "video_source",
        ["enabled", "source_paused"],
    )


def downgrade() -> None:
    op.drop_index("idx_webhook_config_enabled", table_name="webhook_config")
    op.drop_index("idx_event_tag_rel_event", table_name="event_tag_rel")
    op.drop_index("idx_video_session_file_rel_sort", table_name="video_session_file_rel")
    op.drop_index("idx_home_entity_profile_type_enabled", table_name="home_entity_profile")
    op.drop_index("idx_tag_definition_type_enabled", table_name="tag_definition")
    op.drop_index("idx_daily_summary_generated_at", table_name="daily_summary")
    op.drop_index("idx_video_source_enabled_paused", table_name="video_source")
