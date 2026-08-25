"""Make media availability and source aggregate deletion explicit.

Revision ID: 20260826_0017
Revises: 20260826_0016
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "20260826_0017"
down_revision: Union[str, None] = "20260826_0016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _replace_fk(table: str, column: str, referred_table: str, ondelete: str) -> None:
    op.drop_constraint(f"{table}_{column}_fkey", table, type_="foreignkey")
    op.create_foreign_key(
        f"fk_{table}_{column}_{referred_table}",
        table,
        referred_table,
        [column],
        ["id"],
        ondelete=ondelete,
    )


def _assert_no_existing_orphans() -> None:
    bind = op.get_bind()
    checks = (
        ("video_file.source_id", "video_file", "source_id", "video_source"),
        ("video_session.source_id", "video_session", "source_id", "video_source"),
        (
            "video_source_runtime_state.source_id",
            "video_source_runtime_state",
            "source_id",
            "video_source",
        ),
        (
            "video_session_file_rel.session_id",
            "video_session_file_rel",
            "session_id",
            "video_session",
        ),
        (
            "video_session_file_rel.video_file_id",
            "video_session_file_rel",
            "video_file_id",
            "video_file",
        ),
        ("event_record.source_id", "event_record", "source_id", "video_source"),
        ("event_record.session_id", "event_record", "session_id", "video_session"),
        ("event_tag_rel.event_id", "event_tag_rel", "event_id", "event_record"),
        (
            "session_analysis_checkpoint.session_id",
            "session_analysis_checkpoint",
            "session_id",
            "video_session",
        ),
    )
    reports: list[str] = []
    for label, child_table, child_column, parent_table in checks:
        count = bind.execute(
            sa.text(
                f"SELECT count(*) FROM {child_table} child "
                f"LEFT JOIN {parent_table} parent ON parent.id = child.{child_column} "
                "WHERE parent.id IS NULL"
            )
        ).scalar_one()
        if count:
            reports.append(f"{label}={count}")
    if reports:
        report = ", ".join(reports)
        raise RuntimeError(f"Source deletion preflight found existing orphans: {report}")


def upgrade() -> None:
    _assert_no_existing_orphans()
    op.add_column(
        "video_file",
        sa.Column("file_missing", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("video_file", sa.Column("missing_at", sa.DateTime(timezone=True), nullable=True))
    _replace_fk("video_file", "source_id", "video_source", "CASCADE")
    _replace_fk("video_session", "source_id", "video_source", "RESTRICT")
    _replace_fk("video_source_runtime_state", "source_id", "video_source", "CASCADE")
    _replace_fk("video_session_file_rel", "session_id", "video_session", "CASCADE")
    _replace_fk("video_session_file_rel", "video_file_id", "video_file", "CASCADE")
    _replace_fk("event_record", "source_id", "video_source", "RESTRICT")
    _replace_fk("event_record", "session_id", "video_session", "CASCADE")
    _replace_fk("event_tag_rel", "event_id", "event_record", "CASCADE")
    op.alter_column("video_file", "file_missing", server_default=None)


def downgrade() -> None:
    _replace_fk("event_tag_rel", "event_id", "event_record", "NO ACTION")
    _replace_fk("event_record", "session_id", "video_session", "NO ACTION")
    _replace_fk("event_record", "source_id", "video_source", "NO ACTION")
    _replace_fk("video_session_file_rel", "video_file_id", "video_file", "NO ACTION")
    _replace_fk("video_session_file_rel", "session_id", "video_session", "NO ACTION")
    _replace_fk("video_source_runtime_state", "source_id", "video_source", "NO ACTION")
    _replace_fk("video_session", "source_id", "video_source", "NO ACTION")
    _replace_fk("video_file", "source_id", "video_source", "NO ACTION")
    op.drop_column("video_file", "missing_at")
    op.drop_column("video_file", "file_missing")
