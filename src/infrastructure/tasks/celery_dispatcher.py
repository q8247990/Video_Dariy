from datetime import datetime
from typing import Optional

from src.application.pipeline.commands import (
    AnalyzeSessionCommand,
    GenerateDailySummaryCommand,
    SendWebhookCommand,
    SessionBuildCommand,
)
from src.application.ports.task_dispatcher import TaskDispatcherPort
from src.core.celery_app import celery_app
from src.db.session import SessionLocal
from src.models.task_log import TaskLog
from src.services.pipeline_constants import ScanMode, TaskStatus, TaskType
from src.services.task_dispatch_control import (
    build_dedupe_key,
    create_pending_task_log,
    ensure_dict_detail,
    find_duplicate_active_task,
)


class CeleryTaskDispatcher(TaskDispatcherPort):
    def _enqueue_with_dedupe(
        self,
        *,
        task_name: str,
        args: list | None,
        kwargs: dict | None,
        task_type: str,
        task_target_id: int | None,
        detail_json: dict,
        queue: str | None = None,
    ) -> Optional[str]:
        db = SessionLocal()
        pending_log: TaskLog | None = None
        try:
            dedupe_key = build_dedupe_key(task_type, task_target_id, detail_json)
            detail_payload = ensure_dict_detail(detail_json)
            detail_payload["dedupe_key"] = dedupe_key

            duplicate = find_duplicate_active_task(db, task_type, task_target_id, dedupe_key)
            if duplicate:
                return duplicate.queue_task_id or str(duplicate.id)

            pending_log, created = create_pending_task_log(
                db,
                task_type=task_type,
                task_target_id=task_target_id,
                detail_json=detail_payload,
            )
            if not created:
                return pending_log.queue_task_id or str(pending_log.id)

            send_kwargs: dict = {"args": args or [], "kwargs": kwargs or {}}
            if queue:
                send_kwargs["queue"] = queue

            task = celery_app.send_task(task_name, **send_kwargs)
            pending_log.queue_task_id = str(task.id)
            pending_log.message = "Queued"
            db.commit()
            return str(task.id)
        except Exception as exc:
            if isinstance(pending_log, TaskLog):
                db.rollback()
                pending_log.status = TaskStatus.FAILED
                pending_log.finished_at = datetime.now()
                pending_log.message = f"Failed to enqueue: {exc}"
                db.add(pending_log)
                db.commit()
            raise
        finally:
            db.close()

    def dispatch_session_build(self, command: SessionBuildCommand) -> Optional[str]:
        if command.scan_mode == ScanMode.FULL:
            task_name = "src.tasks.session_build.full_build_task"
        else:
            task_name = "src.tasks.session_build.hot_build_task"

        detail_json = {"scan_mode": command.scan_mode, "source_id": command.source_id}
        return self._enqueue_with_dedupe(
            task_name=task_name,
            args=[],
            kwargs={"source_id": command.source_id},
            task_type=TaskType.SESSION_BUILD,
            task_target_id=command.source_id,
            detail_json=detail_json,
        )

    def dispatch_analyze_session(self, command: AnalyzeSessionCommand) -> Optional[str]:
        queue = "analysis_hot" if command.priority == ScanMode.HOT else "analysis_full"
        return self._enqueue_with_dedupe(
            task_name="src.tasks.analyzer.analyze_session_task",
            args=[command.session_id],
            kwargs={"priority": command.priority},
            task_type=TaskType.SESSION_ANALYSIS,
            task_target_id=command.session_id,
            detail_json={"priority": command.priority},
            queue=queue,
        )

    def dispatch_generate_daily_summary(
        self, command: GenerateDailySummaryCommand
    ) -> Optional[str]:
        args = [] if command.target_date_str is None else [command.target_date_str]
        return self._enqueue_with_dedupe(
            task_name="src.tasks.summarizer.generate_daily_summary_task",
            args=args,
            kwargs={},
            task_type=TaskType.DAILY_SUMMARY_GENERATION,
            task_target_id=None,
            detail_json={"target_date": command.target_date_str},
        )

    def dispatch_webhook(self, command: SendWebhookCommand) -> Optional[str]:
        task = celery_app.send_task(
            "src.tasks.webhook.send_webhook_task",
            kwargs={"event_type": command.event_type, "payload": command.payload},
        )
        return str(task.id)
