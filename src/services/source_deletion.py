from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session


@dataclass(frozen=True)
class SourceOrphanReport:
    relation: str
    count: int


def collect_source_deletion_orphan_report(db: Session) -> list[SourceOrphanReport]:
    checks = (
        ("video_file.source_id", "video_file", "source_id", "video_source"),
        ("video_session.source_id", "video_session", "source_id", "video_source"),
        (
            "video_source_runtime_state.source_id",
            "video_source_runtime_state",
            "source_id",
            "video_source",
        ),
        ("event_record.source_id", "event_record", "source_id", "video_source"),
        ("event_record.session_id", "event_record", "session_id", "video_session"),
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
        ("event_tag_rel.event_id", "event_tag_rel", "event_id", "event_record"),
        (
            "session_analysis_checkpoint.session_id",
            "session_analysis_checkpoint",
            "session_id",
            "video_session",
        ),
    )
    reports: list[SourceOrphanReport] = []
    for relation, child_table, child_column, parent_table in checks:
        count = db.execute(
            text(
                f"SELECT count(*) FROM {child_table} child "
                f"LEFT JOIN {parent_table} parent ON parent.id = child.{child_column} "
                "WHERE parent.id IS NULL"
            )
        ).scalar_one()
        reports.append(SourceOrphanReport(relation=relation, count=count))
    return reports
