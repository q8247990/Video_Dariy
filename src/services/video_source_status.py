from datetime import datetime
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from src.models.task_log import TaskLog
from src.models.video_file import VideoFile
from src.models.video_session import VideoSession
from src.models.video_session_file_rel import VideoSessionFileRel
from src.models.video_source import VideoSource
from src.services.pipeline_constants import SessionAnalysisStatus, TaskStatus, TaskType


def build_video_source_status(db: Session, source_id: int) -> dict:
    return build_video_sources_status_map(db, [source_id])[source_id]


def build_video_sources_status_map(db: Session, source_ids: list[int]) -> dict[int, dict]:
    unique_ids = sorted(set(source_ids))
    if not unique_ids:
        return {}

    now = datetime.now()
    video_ranges = _query_video_time_range_map(db, unique_ids)
    analyzed_ranges = _query_analyzed_time_range_map(db, unique_ids)
    total_file_seconds_map = _query_total_file_seconds_map(db, unique_ids)
    analyzed_file_seconds_map = _query_analyzed_file_seconds_map(db, unique_ids)
    analysis_state_map = _query_analysis_state_map(db, unique_ids)
    full_build_running_map = _query_full_build_running_map(db, unique_ids)

    status_map: dict[int, dict] = {}
    for source_id in unique_ids:
        video_earliest_time, video_latest_time = video_ranges.get(source_id, (None, None))
        analyzed_earliest_time, analyzed_latest_time = analyzed_ranges.get(source_id, (None, None))
        total_file_seconds = total_file_seconds_map.get(source_id, 0.0)
        analyzed_file_seconds = analyzed_file_seconds_map.get(source_id, 0.0)

        status_map[source_id] = {
            "source_id": source_id,
            "video_earliest_time": video_earliest_time,
            "video_latest_time": video_latest_time,
            "analyzed_earliest_time": analyzed_earliest_time,
            "analyzed_latest_time": analyzed_latest_time,
            "analyzed_coverage_percent": _calculate_analyzed_coverage_percent(
                analyzed_file_seconds,
                total_file_seconds,
            ),
            "analysis_state": analysis_state_map.get(source_id, "stopped"),
            "minutes_since_last_new_video": _minutes_since_last_new_video(video_latest_time, now),
            "full_build_running": full_build_running_map.get(source_id, False),
            "updated_at": now,
        }

    return status_map


def _query_source_map(db: Session, source_ids: list[int]) -> dict[int, VideoSource]:
    rows = db.query(VideoSource).filter(VideoSource.id.in_(source_ids)).all()
    return {int(row.id): row for row in rows}


def _query_full_build_running_map(db: Session, source_ids: list[int]) -> dict[int, bool]:
    """Check if a full build task is running for each source."""
    rows = (
        db.query(TaskLog)
        .filter(
            TaskLog.task_type == TaskType.SESSION_BUILD,
            TaskLog.task_target_id.in_(source_ids),
            TaskLog.status.in_([TaskStatus.PENDING, TaskStatus.RUNNING]),
        )
        .all()
    )
    result: dict[int, bool] = {}
    for row in rows:
        if row.task_target_id is None:
            continue
        detail = row.detail_json if isinstance(row.detail_json, dict) else {}
        if detail.get("scan_mode") == "full":
            result[int(row.task_target_id)] = True
    return result


def _query_video_time_range_map(
    db: Session,
    source_ids: list[int],
) -> dict[int, tuple[Optional[datetime], Optional[datetime]]]:
    rows = (
        db.query(
            VideoFile.source_id,
            func.min(VideoFile.start_time).label("video_earliest_time"),
            func.max(VideoFile.end_time).label("video_latest_time"),
        )
        .filter(VideoFile.source_id.in_(source_ids))
        .group_by(VideoFile.source_id)
        .all()
    )
    result: dict[int, tuple[Optional[datetime], Optional[datetime]]] = {}
    for row in rows:
        result[int(row.source_id)] = (row.video_earliest_time, row.video_latest_time)
    return result


def _query_analyzed_time_range_map(
    db: Session,
    source_ids: list[int],
) -> dict[int, tuple[Optional[datetime], Optional[datetime]]]:
    rows = (
        db.query(
            VideoSession.source_id,
            func.min(VideoSession.session_start_time).label("analyzed_earliest_time"),
            func.max(VideoSession.session_end_time).label("analyzed_latest_time"),
        )
        .filter(
            VideoSession.source_id.in_(source_ids),
            VideoSession.analysis_status == SessionAnalysisStatus.SUCCESS,
        )
        .group_by(VideoSession.source_id)
        .all()
    )
    result: dict[int, tuple[Optional[datetime], Optional[datetime]]] = {}
    for row in rows:
        result[int(row.source_id)] = (row.analyzed_earliest_time, row.analyzed_latest_time)
    return result


def _query_total_file_seconds_map(
    db: Session,
    source_ids: list[int],
) -> dict[int, float]:
    rows = (
        db.query(
            VideoFile.source_id,
            VideoFile.duration_seconds,
            VideoFile.start_time,
            VideoFile.end_time,
        )
        .filter(VideoFile.source_id.in_(source_ids))
        .all()
    )
    totals: dict[int, float] = {}
    for row in rows:
        source_id = int(row.source_id)
        totals[source_id] = totals.get(source_id, 0.0) + _resolve_file_seconds(
            row.duration_seconds,
            row.start_time,
            row.end_time,
        )
    return totals


def _query_analyzed_file_seconds_map(db: Session, source_ids: list[int]) -> dict[int, float]:
    success_file_subquery = (
        db.query(VideoSessionFileRel.video_file_id.label("video_file_id"))
        .join(VideoSession, VideoSession.id == VideoSessionFileRel.session_id)
        .filter(
            VideoSession.source_id.in_(source_ids),
            VideoSession.analysis_status == SessionAnalysisStatus.SUCCESS,
        )
        .distinct()
        .subquery()
    )

    rows = (
        db.query(
            VideoFile.source_id,
            VideoFile.duration_seconds,
            VideoFile.start_time,
            VideoFile.end_time,
        )
        .join(success_file_subquery, success_file_subquery.c.video_file_id == VideoFile.id)
        .filter(VideoFile.source_id.in_(source_ids))
        .all()
    )

    totals: dict[int, float] = {}
    for row in rows:
        source_id = int(row.source_id)
        totals[source_id] = totals.get(source_id, 0.0) + _resolve_file_seconds(
            row.duration_seconds,
            row.start_time,
            row.end_time,
        )
    return totals


def _resolve_file_seconds(
    duration_seconds: int | float | None,
    start_time: Optional[datetime],
    end_time: Optional[datetime],
) -> float:
    if duration_seconds is not None:
        return float(max(duration_seconds, 0))
    if start_time is None or end_time is None:
        return 0.0
    return max(0.0, (end_time - start_time).total_seconds())


def _calculate_analyzed_coverage_percent(
    analyzed_file_seconds: float,
    total_file_seconds: float,
) -> Optional[float]:
    if total_file_seconds <= 0:
        return None
    percent = (analyzed_file_seconds / total_file_seconds) * 100
    return round(min(max(percent, 0.0), 100.0), 2)


def _minutes_since_last_new_video(
    video_latest_time: Optional[datetime],
    now: datetime,
) -> Optional[int]:
    if video_latest_time is None:
        return None
    delta_seconds = max(0.0, (now - video_latest_time).total_seconds())
    return int(delta_seconds // 60)


def _query_analysis_state_map(db: Session, source_ids: list[int]) -> dict[int, str]:
    states: dict[int, str] = {}

    paused_rows = (
        db.query(VideoSource.id)
        .filter(VideoSource.id.in_(source_ids), VideoSource.source_paused.is_(True))
        .all()
    )
    for row in paused_rows:
        states[int(row[0])] = "paused"

    pending_ids = [source_id for source_id in source_ids if source_id not in states]
    if pending_ids:
        running_logs = (
            db.query(TaskLog.task_target_id)
            .filter(
                TaskLog.task_target_id.in_(pending_ids),
                TaskLog.task_type == TaskType.SESSION_BUILD,
                TaskLog.status == TaskStatus.RUNNING,
            )
            .all()
        )
        for row in running_logs:
            if row[0] is None:
                continue
            source_id = int(row[0])
            states.setdefault(source_id, "analyzing")

    pending_ids = [source_id for source_id in source_ids if source_id not in states]
    if pending_ids:
        analyzing_rows = (
            db.query(VideoSession.source_id)
            .filter(
                VideoSession.source_id.in_(pending_ids),
                VideoSession.analysis_status == SessionAnalysisStatus.ANALYZING,
            )
            .distinct()
            .all()
        )
        for row in analyzing_rows:
            states.setdefault(int(row[0]), "analyzing")

    for source_id in source_ids:
        states.setdefault(source_id, "stopped")
    return states
