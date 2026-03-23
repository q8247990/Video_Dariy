from pathlib import Path
from typing import Any

from src.core.celery_app import celery_app
from src.core.config import settings
from src.db.session import SessionLocal
from src.models.app_runtime_state import AppRuntimeState
from src.models.chat_query_log import ChatQueryLog
from src.models.daily_summary import DailySummary
from src.models.event_record import EventRecord
from src.models.event_tag_rel import EventTagRel
from src.models.mcp_call_log import McpCallLog
from src.models.task_log import TaskLog
from src.models.video_file import VideoFile
from src.models.video_session import VideoSession
from src.models.video_session_file_rel import VideoSessionFileRel
from src.models.video_source import VideoSource
from src.models.video_source_runtime_state import VideoSourceRuntimeState

LEGACY_RUNTIME_KEYS = ("daily_summary_dispatch_guard",)


def _delete_directory_files(root: Path) -> int:
    if not root.exists() or not root.is_dir():
        return 0

    deleted = 0
    for item in sorted(root.rglob("*"), reverse=True):
        if item.is_file() or item.is_symlink():
            item.unlink(missing_ok=True)
            deleted += 1
        elif item.is_dir():
            try:
                item.rmdir()
            except OSError:
                continue
    return deleted


def _clear_celery_tasks() -> dict[str, Any]:  # noqa: C901
    stats: dict[str, Any] = {
        "purged": 0,
        "revoked": 0,
        "active": 0,
        "reserved": 0,
        "scheduled": 0,
    }
    try:
        inspect = celery_app.control.inspect(timeout=1)
        active = inspect.active() or {}
        reserved = inspect.reserved() or {}
        scheduled = inspect.scheduled() or {}

        task_ids: set[str] = set()

        for tasks in active.values():
            stats["active"] += len(tasks)
            for item in tasks:
                task_id = item.get("id")
                if task_id:
                    task_ids.add(str(task_id))

        for tasks in reserved.values():
            stats["reserved"] += len(tasks)
            for item in tasks:
                task_id = item.get("id")
                if task_id:
                    task_ids.add(str(task_id))

        for tasks in scheduled.values():
            stats["scheduled"] += len(tasks)
            for item in tasks:
                request = item.get("request") or {}
                task_id = request.get("id")
                if task_id:
                    task_ids.add(str(task_id))

        for task_id in task_ids:
            celery_app.control.revoke(task_id, terminate=False)

        stats["revoked"] = len(task_ids)
    except Exception:
        pass

    try:
        purged = celery_app.control.purge()
        stats["purged"] = int(purged or 0)
    except Exception:
        pass

    return stats


def main() -> None:
    queue_clear_before = _clear_celery_tasks()

    db = SessionLocal()
    try:
        before = {
            "task_log": db.query(TaskLog).count(),
            "video_file": db.query(VideoFile).count(),
            "video_session": db.query(VideoSession).count(),
            "video_session_file_rel": db.query(VideoSessionFileRel).count(),
            "event_record": db.query(EventRecord).count(),
            "event_tag_rel": db.query(EventTagRel).count(),
            "daily_summary": db.query(DailySummary).count(),
            "chat_query_log": db.query(ChatQueryLog).count(),
            "mcp_call_log": db.query(McpCallLog).count(),
            "video_source_runtime_state": db.query(VideoSourceRuntimeState).count(),
            "app_runtime_state": db.query(AppRuntimeState).count(),
        }

        db.query(EventTagRel).delete(synchronize_session=False)
        db.query(EventRecord).delete(synchronize_session=False)
        db.query(DailySummary).delete(synchronize_session=False)
        db.query(ChatQueryLog).delete(synchronize_session=False)
        db.query(McpCallLog).delete(synchronize_session=False)
        db.query(VideoSessionFileRel).delete(synchronize_session=False)
        db.query(VideoSession).delete(synchronize_session=False)
        db.query(VideoFile).delete(synchronize_session=False)
        db.query(TaskLog).delete(synchronize_session=False)
        db.query(VideoSourceRuntimeState).delete(synchronize_session=False)

        db.query(AppRuntimeState).filter(
            AppRuntimeState.state_key.in_(list(LEGACY_RUNTIME_KEYS))
        ).delete(synchronize_session=False)

        for source in db.query(VideoSource).all():
            source.last_scan_at = None

        db.commit()

        queue_clear_after = _clear_celery_tasks()

        merged_dir = Path(settings.VIDEO_ROOT_PATH) / "session_merged"
        deleted_merged_files = _delete_directory_files(merged_dir)
        deleted_playback_cache_files = _delete_directory_files(Path(settings.PLAYBACK_CACHE_ROOT))

        after = {
            "task_log": db.query(TaskLog).count(),
            "video_file": db.query(VideoFile).count(),
            "video_session": db.query(VideoSession).count(),
            "video_session_file_rel": db.query(VideoSessionFileRel).count(),
            "event_record": db.query(EventRecord).count(),
            "event_tag_rel": db.query(EventTagRel).count(),
            "daily_summary": db.query(DailySummary).count(),
            "chat_query_log": db.query(ChatQueryLog).count(),
            "mcp_call_log": db.query(McpCallLog).count(),
            "video_source_runtime_state": db.query(VideoSourceRuntimeState).count(),
            "app_runtime_state": db.query(AppRuntimeState).count(),
        }

        print("before:", before)
        print("after:", after)
        print("deleted_merged_files:", deleted_merged_files)
        print("deleted_playback_cache_files:", deleted_playback_cache_files)
        print("queue_clear_before:", queue_clear_before)
        print("queue_clear_after:", queue_clear_after)
        print("reset_done: true")
    finally:
        db.close()


if __name__ == "__main__":
    main()
