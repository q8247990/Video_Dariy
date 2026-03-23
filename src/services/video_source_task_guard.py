from sqlalchemy.orm import Session

from src.models.task_log import TaskLog
from src.models.video_session import VideoSession
from src.services.pipeline_constants import SessionAnalysisStatus, TaskStatus, TaskType


def find_running_source_task_type(db: Session, source_id: int) -> str | None:
    running_source_task = (
        db.query(TaskLog.task_type)
        .filter(
            TaskLog.task_target_id == source_id,
            TaskLog.status == TaskStatus.RUNNING,
            TaskLog.task_type == TaskType.SESSION_BUILD,
        )
        .order_by(TaskLog.created_at.desc())
        .first()
    )
    if running_source_task is not None:
        return str(running_source_task[0])

    running_analyzing_session = (
        db.query(VideoSession.id)
        .filter(
            VideoSession.source_id == source_id,
            VideoSession.analysis_status == SessionAnalysisStatus.ANALYZING,
        )
        .first()
    )
    if running_analyzing_session is not None:
        return TaskType.SESSION_ANALYSIS

    return None
