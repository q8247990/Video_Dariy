"""reinterpret legacy Shanghai wall-clock timestamps as UTC instants.

Revision ID: 20260825_0014
Revises: 20260825_0013

Run only during a maintenance write freeze after a verified backup.  This is
intentionally irreversible: downgrading would discard the semantic decision
that legacy naive values are Asia/Shanghai wall-clock values. Restore the
pre-upgrade backup instead.
"""

from typing import Final, Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "20260825_0014"
down_revision: Union[str, None] = "20260825_0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

LEGACY_TIME_ZONE: Final = "Asia/Shanghai"
TIMESTAMP_COLUMNS: Final[tuple[tuple[str, str], ...]] = (
    ("admin_user", "last_login_at"),
    ("admin_user", "created_at"),
    ("admin_user", "updated_at"),
    ("app_runtime_state", "created_at"),
    ("app_runtime_state", "updated_at"),
    ("home_entity_profile", "created_at"),
    ("home_entity_profile", "updated_at"),
    ("home_profile", "created_at"),
    ("home_profile", "updated_at"),
    ("llm_provider", "last_test_at"),
    ("llm_provider", "created_at"),
    ("llm_provider", "updated_at"),
    ("mcp_call_log", "created_at"),
    ("mcp_call_log", "updated_at"),
    ("system_config", "created_at"),
    ("system_config", "updated_at"),
    ("tag_definition", "created_at"),
    ("tag_definition", "updated_at"),
    ("task_log", "started_at"),
    ("task_log", "finished_at"),
    ("task_log", "created_at"),
    ("task_log", "updated_at"),
    ("video_source", "paused_at"),
    ("video_source", "last_scan_at"),
    ("video_source", "last_validate_at"),
    ("video_source", "created_at"),
    ("video_source", "updated_at"),
    ("webhook_config", "created_at"),
    ("webhook_config", "updated_at"),
    ("chat_query_log", "created_at"),
    ("chat_query_log", "updated_at"),
    ("daily_summary", "generated_at"),
    ("daily_summary", "created_at"),
    ("daily_summary", "updated_at"),
    ("llm_usage_log", "created_at"),
    ("llm_usage_log", "updated_at"),
    ("video_file", "start_time"),
    ("video_file", "end_time"),
    ("video_file", "created_at"),
    ("video_file", "updated_at"),
    ("video_session", "session_start_time"),
    ("video_session", "session_end_time"),
    ("video_session", "last_analyzed_at"),
    ("video_session", "created_at"),
    ("video_session", "updated_at"),
    ("video_source_runtime_state", "latency_alert_last_notified_at"),
    ("video_source_runtime_state", "created_at"),
    ("video_source_runtime_state", "updated_at"),
    ("event_record", "event_start_time"),
    ("event_record", "event_end_time"),
    ("event_record", "created_at"),
    ("event_record", "updated_at"),
    ("video_session_file_rel", "created_at"),
    ("video_session_file_rel", "updated_at"),
    ("event_tag_rel", "created_at"),
    ("event_tag_rel", "updated_at"),
    ("webhook_delivery_log", "delivered_at"),
    ("webhook_delivery_log", "created_at"),
    ("webhook_delivery_log", "updated_at"),
)


def _column_type(table: str, column: str) -> str | None:
    return (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT data_type FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = :table "
                "AND column_name = :column"
            ),
            {"table": table, "column": column},
        )
        .scalar_one_or_none()
    )


def upgrade() -> None:
    for table, column in TIMESTAMP_COLUMNS:
        column_type = _column_type(table, column)
        if column_type != "timestamp without time zone":
            raise RuntimeError(
                f"timestamp migration preflight failed for {table}.{column}: "
                f"expected timestamp without time zone, got {column_type!r}; restore backup or stop"
            )
        op.execute(
            sa.text(
                f'ALTER TABLE "{table}" ALTER COLUMN "{column}" '
                f'TYPE TIMESTAMP WITH TIME ZONE USING "{column}" '
                f"AT TIME ZONE '{LEGACY_TIME_ZONE}'"
            )
        )


def downgrade() -> None:
    raise RuntimeError(
        "20260825_0014 is irreversible; restore the verified pre-migration backup "
        "instead of downgrading."
    )
