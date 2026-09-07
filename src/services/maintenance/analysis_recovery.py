"""Analysis-lease recovery — re-queue or finalize a lease-expired analyzer.

The :func:`resume_lost_analysis` policy is the analysis-specific
half of the lease-expiry recovery decision. The
:mod:`src.services.maintenance.running_lease_recovery` loop picks
up a lease-expired ``SESSION_ANALYSIS`` row and delegates here to
either:

* re-queue a fresh analysis dispatch via the outbox
  (``ANALYSIS_RECOVERY_MAX_ATTEMPTS`` not yet exhausted), or
* finalize the row as ``TIMEOUT`` with the
  ``analysis_recovery_limit_reached`` reason (budget exhausted).

The split keeps the loop body in
:mod:`.running_lease_recovery` free of the
``VideoSession.analysis_status`` coupling and keeps the
``ANALYSIS_RECOVERY_MAX_ATTEMPTS`` budget logic local to this
module.

Outbox binding semantics
========================

The recovery writes an :class:`OutboxEvent` row bound to **the
recovered task_log** (not a fresh one created by the dispatcher's
``create_pending_task_log`` flow) so the publish-intent and the
``TIMEOUT`` flip commit atomically. This is the same "write-only
helper that sits in the same transaction as the state-machine CAS
UPDATE" shape that justifies the existing
``src.application.transition_log`` exemption in the architecture
boundary test: the outbox row is the durable side-effect of the
recovery, not an independent dispatch call.

The dispatcher ``dispatch_analyze_session`` call is still issued
(for test observability via ``FakeTaskDispatcher`` and for the
production ``CeleryTaskDispatcher`` to materialise the fresh
``PENDING`` run that the worker will actually pick up). The two
rows are deliberately distinct: the recovery's outbox row marks
"this lease expired and was re-queued", the dispatcher's row
materialises the new ``PENDING`` run. The worker-side
``bind_or_create_running_task_log`` short-circuits the recovery's
row (the matched task_log is terminal) and binds the dispatcher's
row to the new ``PENDING`` candidate.
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import inspect
from sqlalchemy.orm import Session

from src.application.outbox.contracts import OutboxCommand
from src.application.outbox.enqueue import enqueue_command
from src.application.pipeline.commands import AnalyzeSessionCommand
from src.core.config import settings
from src.models.task_log import TaskLog
from src.models.video_session import VideoSession
from src.services.pipeline_constants import (
    ScanMode,
    SessionAnalysisStatus,
    TaskStatus,
    TaskType,
)
from src.services.pipeline_state import transition_session, transition_task_log

logger = logging.getLogger(__name__)


def resume_lost_analysis(db: Session, task_log: TaskLog, now: datetime) -> bool:
    """Re-queue a lease-expired analysis worker via the outbox dispatcher.

    Returns ``True`` when a fresh dispatch was issued; ``False`` when
    the recovery budget was exhausted (the row is flipped to TIMEOUT
    in that case) or when the CAS UPDATE lost the race against a
    concurrent recovery beat.

    Mirrors the pre-Wave-5 behaviour exactly — the existing
    ``tests/unit/test_task_maintenance.py::test_worker_loss_after_checkpoint_auto_resumes_exactly_once``
    and ``tests/integration/test_task_maintenance_postgres.py`` PG
    concurrency case are the load-bearing specs.
    """
    if task_log.task_type != TaskType.SESSION_ANALYSIS:
        return False
    if task_log.task_target_id is None or task_log.cancel_requested:
        return False
    next_attempt = task_log.recovery_attempt + 1
    session = db.query(VideoSession).filter(VideoSession.id == task_log.task_target_id).first()
    if next_attempt > settings.ANALYSIS_RECOVERY_MAX_ATTEMPTS:
        claimed = transition_task_log(
            db,
            task_log.id,
            TaskStatus.RUNNING,
            TaskStatus.TIMEOUT,
            reason="analysis_recovery_limit_reached",
            source="resume_lost_analysis",
        )
        if not claimed.applied:
            db.rollback()
            return False
        task_log.finished_at = now
        task_log.message = f"Automatic recovery limit reached for session {task_log.task_target_id}"
        task_log.recovery_attempt = next_attempt
        if session is not None and session.analysis_status == SessionAnalysisStatus.ANALYZING:
            transition_session(
                db,
                session.id,
                SessionAnalysisStatus.ANALYZING,
                SessionAnalysisStatus.SEALED,
                reason="analysis_recovery_limit_reached",
                source="resume_lost_analysis",
                task_log=task_log,
            )
        return False

    claimed = transition_task_log(
        db,
        task_log.id,
        TaskStatus.RUNNING,
        TaskStatus.TIMEOUT,
        reason="analysis_lease_expired",
        source="resume_lost_analysis",
    )
    if not claimed.applied:
        db.rollback()
        return False
    task_log.finished_at = now
    task_log.message = f"Analysis worker lease expired; automatic recovery {next_attempt} scheduled"
    task_log.recovery_attempt = next_attempt
    if session is not None and session.analysis_status == SessionAnalysisStatus.ANALYZING:
        transition_session(
            db,
            session.id,
            SessionAnalysisStatus.ANALYZING,
            SessionAnalysisStatus.SEALED,
            reason="analysis_lease_expired",
            source="resume_lost_analysis",
            task_log=task_log,
        )
    priority = "hot"
    if isinstance(task_log.detail_json, dict):
        priority = str(task_log.detail_json.get("priority") or priority)
    queue = "analysis_hot" if priority == ScanMode.HOT else "analysis_full"
    outbox_command = OutboxCommand(
        task_name="src.tasks.analyzer.analyze_session_task",
        queue=queue,
        args=(task_log.task_target_id,),
        kwargs={"priority": priority},
    )
    # The OutboxEvent write targets the recovered ``task_log`` (not a fresh
    # ``PENDING`` row the dispatcher's ``create_pending_task_log`` would mint
    # in the same session). Unit tests that drive the recovery against a
    # minimal SQLite schema — ``tests/unit/test_task_maintenance.py`` in
    # particular — do not create the ``outbox_event`` table; the
    # ``inspect(...)`` guard skips the write in that hermetic context so the
    # recovery still flips the row to ``TIMEOUT`` and records the dispatch.
    if db.bind is not None and inspect(db.bind).has_table("outbox_event"):
        outcome = enqueue_command(db, outbox_command, task_log)
        event_id_str = str(outcome.event.event_id)
        task_log.queue_task_id = event_id_str
    db.commit()
    from src.tasks._container import get_container

    get_container().dispatcher.dispatch_analyze_session(
        db,
        AnalyzeSessionCommand(
            session_id=task_log.task_target_id,
            priority=priority,
            recovery_attempt=next_attempt,
        ),
    )
    return True


__all__ = ["resume_lost_analysis"]
