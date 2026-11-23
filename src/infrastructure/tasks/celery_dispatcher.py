from typing import Any

from sqlalchemy.orm import Session

from src.application.pipeline.commands import (
    AnalyzeSessionCommand,
    GenerateDailySummaryCommand,
    SendWebhookCommand,
    SessionBuildCommand,
)
from src.application.ports.task_dispatcher import TaskDispatcherPort
from src.core.pipeline_constants import ScanMode, TaskStatus, TaskType
from src.models.task_log import TaskLog
from src.services.dispatch import (
    create_pending_task_log,
    ensure_dict_detail,
    find_duplicate_active_task,
    record_deferred_hot_scan,
    supersede_active_hot_scan,
)
from src.services.dispatch.outbox import new_task_id, record_delivery
from src.services.dispatch.task_command import TaskCommand

DEFAULT_QUEUE: str = "celery"


class CeleryTaskDispatcher(TaskDispatcherPort):
    """Production :class:`TaskDispatcherPort` adapter.

    Each ``dispatch_*`` call writes the business ``TaskLog`` row, a
    ``TaskDelivery`` intent row and the pre-assigned broker ``task_id`` to
    the caller-supplied ``db`` session without committing. Intents are
    collected in the :func:`src.services.dispatch.outbox.pending_deliveries`
    context of the calling thread (one dispatcher instance is shared per
    process, so a per-instance list would mix concurrent requests). The
    caller commits and then calls
    :func:`src.services.dispatch.outbox.flush_task_deliveries` with the
    collected list. Without an active collection context the intent stays
    in ``task_delivery`` and the heartbeat's retry sweep publishes it.
    """

    def _stage(
        self,
        db: Session,
        task_log: TaskLog,
        *,
        task_name: str,
        queue: str,
        args: tuple[Any, ...] = (),
        kwargs: dict[str, Any] | None = None,
    ) -> str:
        task_id = new_task_id()
        record_delivery(
            db,
            task_log,
            TaskCommand(task_name=task_name, queue=queue, args=args, kwargs=kwargs),
            task_id=task_id,
        )
        return task_id

    def dispatch_session_build(self, db: Session, command: SessionBuildCommand) -> str | None:
        """Enqueue a hot or full session build.

        Hot-over-full is recorded as a deferred (skipped) TaskLog; the
        active FULL already owns its delivery, so no second intent is
        created. Full-over-hot supersedes the HOT and publishes the FULL.
        """
        task_name = (
            "src.tasks.session_build.full_build_task"
            if command.scan_mode == ScanMode.FULL
            else "src.tasks.session_build.hot_build_task"
        )
        detail_json = {"scan_mode": command.scan_mode, "source_id": command.source_id}

        pending_log, created = create_pending_task_log(
            db,
            task_type=TaskType.SESSION_BUILD,
            task_target_id=command.source_id,
            detail_json=detail_json,
        )
        db.flush()

        if not created:
            return self._resolve_session_build_dedupe(
                db,
                pending_log=pending_log,
                detail_json=detail_json,
                source_id=command.source_id,
            )

        return self._stage(
            db,
            pending_log,
            task_name=task_name,
            queue=DEFAULT_QUEUE,
            kwargs={"source_id": command.source_id},
        )

    def dispatch_analyze_session(self, db: Session, command: AnalyzeSessionCommand) -> str | None:
        """Enqueue a session analysis on the hot or full analysis queue."""
        queue = "analysis_hot" if command.priority == ScanMode.HOT else "analysis_full"
        detail_json = {
            "priority": command.priority,
            "recovery_attempt": command.recovery_attempt,
        }

        pending_log, created = create_pending_task_log(
            db,
            task_type=TaskType.SESSION_ANALYSIS,
            task_target_id=command.session_id,
            detail_json=detail_json,
        )
        db.flush()

        if not created:
            active = find_duplicate_active_task(
                db,
                TaskType.SESSION_ANALYSIS,
                command.session_id,
                pending_log.dedupe_key or "",
            )
            if active is not None:
                return active.queue_task_id or str(active.id)
            return pending_log.queue_task_id or str(pending_log.id)

        return self._stage(
            db,
            pending_log,
            task_name="src.tasks.analyzer.analyze_session_task",
            queue=queue,
            args=(command.session_id,),
            kwargs={"priority": command.priority},
        )

    def dispatch_generate_daily_summary(
        self, db: Session, command: GenerateDailySummaryCommand
    ) -> str | None:
        """Enqueue a daily-summary generation."""
        args: tuple[Any, ...] = (
            () if command.target_date_str is None else (command.target_date_str,)
        )

        pending_log, created = create_pending_task_log(
            db,
            task_type=TaskType.DAILY_SUMMARY_GENERATION,
            task_target_id=None,
            detail_json={"target_date": command.target_date_str},
        )
        db.flush()

        if not created:
            return pending_log.queue_task_id or str(pending_log.id)

        return self._stage(
            db,
            pending_log,
            task_name="src.tasks.summarizer.generate_daily_summary_task",
            queue=DEFAULT_QUEUE,
            args=args,
        )

    def dispatch_webhook(self, db: Session, command: SendWebhookCommand) -> str | None:
        """Enqueue a webhook delivery.

        Webhooks do not participate in the active-dedupe pipeline: every
        subscriber delivery is its own task log, so ``dedupe_key`` stays
        ``None``. When ``command.webhook_id`` is set the consumer receives a
        targeted ``webhook_id`` kwarg and delivers to exactly that subscriber.
        """
        detail_json: dict[str, Any] = {
            "event_type": command.event_type,
            "payload_keys": sorted((command.payload or {}).keys()),
        }
        kwargs: dict[str, Any] = {
            "event_type": command.event_type,
            "payload": command.payload,
        }
        if command.webhook_id is not None:
            kwargs["webhook_id"] = int(command.webhook_id)
            detail_json["webhook_id"] = int(command.webhook_id)

        pending_log = TaskLog(
            task_type=TaskType.WEBHOOK_PUSH,
            task_target_id=command.webhook_id,
            dedupe_key=None,
            status=TaskStatus.PENDING,
            detail_json=detail_json,
        )
        db.add(pending_log)
        db.flush()

        return self._stage(
            db,
            pending_log,
            task_name="src.tasks.webhook.send_webhook_task",
            queue=DEFAULT_QUEUE,
            kwargs=kwargs,
        )

    def _resolve_session_build_dedupe(
        self,
        db: Session,
        *,
        pending_log: TaskLog,
        detail_json: dict[str, Any],
        source_id: int,
    ) -> str | None:
        """Apply the HOT/FULL precedence rule and return the authoritative id."""
        detail_payload = ensure_dict_detail(detail_json)
        active_scan_mode = ensure_dict_detail(pending_log.detail_json).get("scan_mode")
        requested_scan_mode = detail_payload.get("scan_mode")

        if requested_scan_mode == ScanMode.HOT and active_scan_mode == ScanMode.FULL:
            deferred = record_deferred_hot_scan(db, source_id, pending_log)
            return str(deferred.id)

        if requested_scan_mode == ScanMode.FULL and active_scan_mode == ScanMode.HOT:
            supersede_active_hot_scan(db, source_id, pending_log)
            replacement, created = create_pending_task_log(
                db,
                task_type=TaskType.SESSION_BUILD,
                task_target_id=source_id,
                detail_json=detail_payload,
            )
            db.flush()
            if not created:
                raise RuntimeError(
                    f"Full scan claim not created after hot supersession for source {source_id}"
                )
            return self._stage(
                db,
                replacement,
                task_name="src.tasks.session_build.full_build_task",
                queue=DEFAULT_QUEUE,
                kwargs={"source_id": source_id},
            )

        return pending_log.queue_task_id or str(pending_log.id)


__all__ = ["CeleryTaskDispatcher", "DEFAULT_QUEUE"]
