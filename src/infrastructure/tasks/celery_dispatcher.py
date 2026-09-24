"""Celery adapter for :class:`~src.application.ports.task_dispatcher.TaskDispatcherPort`.

This is the **only** ``src.infrastructure.*`` adapter allowed to import
the outbox seam (``src.application.outbox.enqueue`` /
``src.application.outbox.contracts``). It is the composition-root
binding point for the outbox adapter — the architecture-boundary test
explicitly whitelists the ``src.infrastructure.tasks`` →
``src.application.outbox`` import as the legitimate bridge.

Cutover (Todo 14)
=================

Under the previous dispatcher every dispatch call opened a private
``SessionLocal`` and called ``celery_app.send_task`` directly. The
transactional outbox (Wave 3, Todos 11-13) replaced that pipeline with
an in-DB publish intent: the dispatcher now writes a ``TaskLog`` row
and an ``OutboxEvent`` row **in the caller's transaction**, returns the
``OutboxEvent.event_id`` as the Celery ``task_id``, and the standalone
outbox publisher (Todo 13) is the single process that ever pushes
``send_task`` to the broker.

Atomicity contract
==================

* ``db`` is caller-owned; the dispatcher never opens, commits or
  rolls back a session on the caller's behalf.
* ``enroll_with_task_log`` runs in the caller's transaction so a
  ``commit()`` makes ``TaskLog`` + ``OutboxEvent`` visible atomically;
  a ``rollback()`` leaves zero rows behind. The PG ``partial unique
  index (task_log_id) WHERE status='pending'`` is the concurrency
  safety net.
* The dispatcher sets ``TaskLog.queue_task_id = str(event_id)`` so the
  consumer-side ``bind_or_create_running_task_log(queue_task_id=...)``
  short-circuit matches the publisher's broker ``task_id``.

Hot/full scan semantics (preserved)
===================================

* HOT over an active FULL → record a deferred (skipped) TaskLog; do
  not publish.
* FULL over an active HOT → supersede the HOT (cancel it), then publish
  the FULL.
* Otherwise → reuse the existing active TaskLog's ``queue_task_id`` and
  return it; no outbox row is created (the existing row's outbox publish
  intent is the authoritative one).
"""

from __future__ import annotations

from typing import Any, Optional

from sqlalchemy.orm import Session

from src.application.outbox.contracts import OutboxCommand
from src.application.outbox.enqueue import enqueue_command
from src.application.pipeline.commands import (
    AnalyzeSessionCommand,
    GenerateDailySummaryCommand,
    SendWebhookCommand,
    SessionBuildCommand,
)
from src.application.ports.task_dispatcher import TaskDispatcherPort
from src.models.task_log import TaskLog
from src.services.pipeline_constants import ScanMode, TaskType
from src.services.task_dispatch_control import (
    create_pending_task_log,
    ensure_dict_detail,
    find_duplicate_active_task,
    record_deferred_hot_scan,
    supersede_active_hot_scan,
)

# Default Celery queue the dispatcher uses when a command does not
# specify one. The outbox registry also has its own default; the two
# stay in sync because the registry is the whitelist and the dispatcher
# always passes an explicit ``queue`` for the analyzer's hot/full
# distinction. Keep this constant importable for tests that want to
# assert the default.
DEFAULT_QUEUE: str = "celery"


class CeleryTaskDispatcher(TaskDispatcherPort):
    """Production :class:`TaskDispatcherPort` adapter.

    Each ``dispatch_*`` call writes the business ``TaskLog`` row and the
    matching ``OutboxEvent`` row to the caller-supplied ``db`` session
    without committing. The caller (``src.tasks`` heartbeat, an API
    endpoint, the recovery path in ``task_maintenance``, …) owns the
    transaction and commits when appropriate.
    """

    # ------------------------------------------------------------------
    # Session build dispatch
    # ------------------------------------------------------------------

    def dispatch_session_build(self, db: Session, command: SessionBuildCommand) -> Optional[str]:
        """Enqueue a hot or full session build via the outbox.

        Hot-over-full is recorded as a deferred (skipped) TaskLog; the
        active FULL already has its own publish intent, so we do NOT
        create a second outbox row. Full-over-hot supersedes the
        HOT and publishes the FULL.
        """
        task_name = (
            "src.tasks.session_build.full_build_task"
            if command.scan_mode == ScanMode.FULL
            else "src.tasks.session_build.hot_build_task"
        )
        detail_json = {"scan_mode": command.scan_mode, "source_id": command.source_id}
        kwargs = {"source_id": command.source_id}

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

        enroll_outcome = enqueue_command(
            db,
            OutboxCommand(task_name=task_name, queue=DEFAULT_QUEUE, kwargs=kwargs),
            pending_log,
        )
        event_id_str = str(enroll_outcome.event.event_id)
        pending_log.queue_task_id = event_id_str
        pending_log.message = "Queued via outbox"
        return event_id_str

    # ------------------------------------------------------------------
    # Session analysis dispatch
    # ------------------------------------------------------------------

    def dispatch_analyze_session(
        self, db: Session, command: AnalyzeSessionCommand
    ) -> Optional[str]:
        """Enqueue a session analysis via the outbox.

        Queue selection honors the priority: ``hot`` → ``analysis_hot``,
        ``full`` → ``analysis_full``. The Celery vision worker pool is
        partitioned by queue so hot vs full do not contend for slots.
        """
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
            # Defensive: a duplicate is reported by the partial unique
            # index but ``find_duplicate_active_task`` did not find it
            # (e.g. SQLite tests without the partial index). Fall back
            # to the ``pending_log`` that came back from the
            # ``create`` helper.
            return pending_log.queue_task_id or str(pending_log.id)

        outcome = enqueue_command(
            db,
            OutboxCommand(
                task_name="src.tasks.analyzer.analyze_session_task",
                queue=queue,
                args=(command.session_id,),
                kwargs={"priority": command.priority},
            ),
            pending_log,
        )
        event_id_str = str(outcome.event.event_id)
        pending_log.queue_task_id = event_id_str
        pending_log.message = "Queued via outbox"
        return event_id_str

    # ------------------------------------------------------------------
    # Daily summary dispatch
    # ------------------------------------------------------------------

    def dispatch_generate_daily_summary(
        self, db: Session, command: GenerateDailySummaryCommand
    ) -> Optional[str]:
        """Enqueue a daily-summary generation via the outbox."""
        args = () if command.target_date_str is None else (command.target_date_str,)
        detail_json = {"target_date": command.target_date_str}

        pending_log, created = create_pending_task_log(
            db,
            task_type=TaskType.DAILY_SUMMARY_GENERATION,
            task_target_id=None,
            detail_json=detail_json,
        )
        db.flush()

        if not created:
            return pending_log.queue_task_id or str(pending_log.id)

        outcome = enqueue_command(
            db,
            OutboxCommand(
                task_name="src.tasks.summarizer.generate_daily_summary_task",
                queue=DEFAULT_QUEUE,
                args=args,
                kwargs={},
            ),
            pending_log,
        )
        event_id_str = str(outcome.event.event_id)
        pending_log.queue_task_id = event_id_str
        pending_log.message = "Queued via outbox"
        return event_id_str

    # ------------------------------------------------------------------
    # Webhook dispatch
    # ------------------------------------------------------------------

    def dispatch_webhook(self, db: Session, command: SendWebhookCommand) -> Optional[str]:
        """Enqueue a webhook delivery via the outbox.

        Webhooks do not participate in the ``TaskLog`` active-dedupe
        pipeline (the broker-level at-least-once semantics are the
        source of truth and the consumer's HTTP layer is responsible
        for retries). We still create a fresh ``TaskLog`` row so the
        outbox row's FK target is non-null and so operators have a
        delivery trail.

        When ``command.webhook_id`` is set, the task receives a
        targeted ``webhook_id`` kwarg so the consumer delivers to
        exactly that one subscriber; the canonical
        ``publish_daily_summary`` use case uses this to enroll one
        outbox row per subscriber and avoids the fan-out duplicate
        path. Without ``webhook_id``, the consumer falls back to the
        legacy fan-out semantics so existing producers that have not
        yet migrated continue to work.
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
            task_type="webhook_delivery",
            task_target_id=command.webhook_id,
            dedupe_key=None,
            status="pending",
            detail_json=detail_json,
        )
        db.add(pending_log)
        db.flush()

        outcome = enqueue_command(
            db,
            OutboxCommand(
                task_name="src.tasks.webhook.send_webhook_task",
                queue=DEFAULT_QUEUE,
                args=(),
                kwargs=kwargs,
            ),
            pending_log,
        )
        event_id_str = str(outcome.event.event_id)
        pending_log.queue_task_id = event_id_str
        pending_log.message = "Queued via outbox"
        return event_id_str

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_session_build_dedupe(
        self,
        db: Session,
        *,
        pending_log: TaskLog,
        detail_json: dict,
        source_id: int,
    ) -> Optional[str]:
        """Apply the HOT↔FULL precedence rule and return the right id.

        Returns the ``queue_task_id`` (or ``id``) of whichever existing
        TaskLog the caller should treat as the authoritative publish
        intent. Never creates a second outbox row.
        """
        detail_payload = ensure_dict_detail(detail_json)
        active_scan_mode = ensure_dict_detail(pending_log.detail_json).get("scan_mode")
        requested_scan_mode = detail_payload.get("scan_mode")

        # HOT over an active FULL → defer; the FULL already owns the
        # publish intent.
        if requested_scan_mode == ScanMode.HOT and active_scan_mode == ScanMode.FULL:
            deferred = record_deferred_hot_scan(db, source_id, pending_log)
            return str(deferred.id)

        # FULL over an active HOT → supersede the HOT and re-publish.
        if requested_scan_mode == ScanMode.FULL and active_scan_mode == ScanMode.HOT:
            supersede_active_hot_scan(db, source_id, pending_log)
            pending_log, created = create_pending_task_log(
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
            outcome = enqueue_command(
                db,
                OutboxCommand(
                    task_name="src.tasks.session_build.full_build_task",
                    queue=DEFAULT_QUEUE,
                    kwargs={"source_id": source_id},
                ),
                pending_log,
            )
            event_id_str = str(outcome.event.event_id)
            pending_log.queue_task_id = event_id_str
            pending_log.message = "Queued via outbox"
            return event_id_str

        # Same scan-mode / no precedence rule → reuse the existing
        # active row's publish intent.
        return pending_log.queue_task_id or str(pending_log.id)


__all__ = ["CeleryTaskDispatcher", "DEFAULT_QUEUE"]
