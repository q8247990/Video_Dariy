from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Case, and_, case, func
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

    now = datetime.now(timezone.utc)
    video_metrics = _query_video_file_metrics_map(db, unique_ids)
    analyzed_ranges = _query_analyzed_time_range_map(db, unique_ids)
    analyzed_file_seconds_map = _query_analyzed_file_seconds_map(db, unique_ids)
    full_build_running_map, running_build_map = _query_session_build_task_map(db, unique_ids)
    analysis_state_map = _query_analysis_state_map(db, unique_ids, running_build_map)

    status_map: dict[int, dict] = {}
    for source_id in unique_ids:
        video_earliest_time, video_latest_time, total_file_seconds = video_metrics.get(
            source_id, (None, None, 0.0)
        )
        analyzed_earliest_time, analyzed_latest_time = analyzed_ranges.get(source_id, (None, None))
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


def _query_session_build_task_map(
    db: Session,
    source_ids: list[int],
) -> tuple[dict[int, bool], dict[int, bool]]:
    """一次查询 task_log，返回 (全量构建运行中, 构建任务运行中) 两个映射。

    两条查询此前都是 ``task_type=SESSION_BUILD`` 且状态在
    (PENDING, RUNNING) 的 task_log 扫描，合并为一次。
    """
    rows = (
        db.query(TaskLog.task_target_id, TaskLog.status, TaskLog.detail_json)
        .filter(
            TaskLog.task_type == TaskType.SESSION_BUILD,
            TaskLog.task_target_id.in_(source_ids),
            TaskLog.status.in_([TaskStatus.PENDING, TaskStatus.RUNNING]),
        )
        .all()
    )
    full_build_running: dict[int, bool] = {}
    running_build: dict[int, bool] = {}
    for target_id, status, detail_json in rows:
        if target_id is None:
            continue
        source_id = int(target_id)
        detail = detail_json if isinstance(detail_json, dict) else {}
        if detail.get("scan_mode") == "full":
            full_build_running[source_id] = True
        if status == TaskStatus.RUNNING:
            running_build[source_id] = True
    return full_build_running, running_build


def _file_seconds_expr() -> Case:
    """单文件秒数的 SQL 表达式，语义与原 Python 版 ``_resolve_file_seconds`` 一致：

    - ``duration_seconds`` 非空时取 ``max(duration_seconds, 0)``
    - 否则回退为 ``max(end_time - start_time, 0)``（秒）
    - 均不可用时为 0

    注意：PostgreSQL 不支持 ``extract(field, expr)`` 逗号语法，
    因此使用 ``date_part('epoch', ...)`` 将 interval 转为秒。
    """
    return case(
        (
            VideoFile.duration_seconds.isnot(None),
            func.greatest(VideoFile.duration_seconds, 0),
        ),
        (
            and_(VideoFile.start_time.isnot(None), VideoFile.end_time.isnot(None)),
            func.greatest(func.date_part("epoch", VideoFile.end_time - VideoFile.start_time), 0),
        ),
        else_=0,
    )


def _query_video_file_metrics_map(
    db: Session,
    source_ids: list[int],
) -> dict[int, tuple[Optional[datetime], Optional[datetime], float]]:
    """一次查询 video_file：时间范围 + 总时长（秒数求和由 SQL 完成）。"""
    rows = (
        db.query(
            VideoFile.source_id,
            func.min(VideoFile.start_time).label("video_earliest_time"),
            func.max(VideoFile.end_time).label("video_latest_time"),
            func.sum(_file_seconds_expr()).label("total_file_seconds"),
        )
        .filter(VideoFile.source_id.in_(source_ids))
        .group_by(VideoFile.source_id)
        .all()
    )
    result: dict[int, tuple[Optional[datetime], Optional[datetime], float]] = {}
    for row in rows:
        result[int(row.source_id)] = (
            row.video_earliest_time,
            row.video_latest_time,
            float(row.total_file_seconds or 0.0),
        )
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


def _query_analyzed_file_seconds_map(db: Session, source_ids: list[int]) -> dict[int, float]:
    """已完成分析文件（去重）的总时长，秒数求和由 SQL 完成。"""
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
            func.sum(_file_seconds_expr()).label("analyzed_file_seconds"),
        )
        .join(success_file_subquery, success_file_subquery.c.video_file_id == VideoFile.id)
        .filter(VideoFile.source_id.in_(source_ids))
        .group_by(VideoFile.source_id)
        .all()
    )

    totals: dict[int, float] = {}
    for row in rows:
        totals[int(row.source_id)] = float(row.analyzed_file_seconds or 0.0)
    return totals


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
    if video_latest_time.tzinfo is None:
        video_latest_time = video_latest_time.replace(tzinfo=timezone.utc)
    delta_seconds = max(0.0, (now - video_latest_time).total_seconds())
    return int(delta_seconds // 60)


def _query_analysis_state_map(
    db: Session,
    source_ids: list[int],
    running_build_map: dict[int, bool],
) -> dict[int, str]:
    """状态优先级：paused > 构建任务 RUNNING（调用方一次查询提供）> session ANALYZING > stopped。"""
    states: dict[int, str] = {}

    paused_rows = (
        db.query(VideoSource.id)
        .filter(VideoSource.id.in_(source_ids), VideoSource.source_paused.is_(True))
        .all()
    )
    for row in paused_rows:
        states[int(row[0])] = "paused"

    for source_id in source_ids:
        if source_id not in states and running_build_map.get(source_id, False):
            states[source_id] = "analyzing"

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
