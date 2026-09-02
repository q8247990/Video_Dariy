"""Worker-bind policy — bind a queue message to a TaskLog row.

The worker-bind helper is the **idempotency seam** where a Celery
``task_id`` (the ``outbox_event.event_id`` UUID string) meets the
``TaskLog`` lifecycle. It is called by every consumer-side Celery
task (``hot_build_task``, ``full_build_task``,
``analyze_session_task``, ``generate_daily_summary_task``,
``send_webhook_task``) right after the worker picks up the message.

The function has three behavioural branches:

1. ``queue_task_id`` is set **and** a matching TaskLog row exists.
   The row is refreshed into RUNNING if it is still active, or the
   helper returns ``None`` if the row is already terminal (the
   "stale queue message" short-circuit that prevents resurrection
   of a finalized row when a redelivered broker message races with
   the recovery policy).
2. Otherwise the helper looks for a pending TaskLog candidate by
   ``(task_type, task_target_id, dedupe_key)`` and binds the
   earliest match.
3. Otherwise the helper creates a fresh RUNNING row in one INSERT
   (PG dialect uses the partial unique index as the silent race
   net; SQLite falls back to ``IntegrityError`` rollback).

Each branch has its own helper in this module so the top-level
function stays at the C901 budget (≤ 10 branches). The behavioural
contract is preserved verbatim from the pre-Wave-5 monolith — the
existing ``tests/unit/test_task_dispatch_binding.py`` is the spec.

Sub-modules:

* :mod:`._state` — :func:`apply_running_state` (mutate a row into
  RUNNING), :func:`mark_superseded_pending` (cancel sibling
  pending candidates sharing the dedupe-key).
* :mod:`._branches` — :func:`bind_existing_queue_row`,
  :func:`bind_pending_candidate`, :func:`insert_fresh_running_row`,
  the three bind branches.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session

from src.core.config import settings
from src.models.task_log import TaskLog
from src.services.dispatch.dedupe import _ensure_dedupe_key, ensure_dict_detail
from src.services.dispatch.worker_bind._branches import (
    bind_existing_queue_row,
    bind_pending_candidate,
    insert_fresh_running_row,
)


def bind_or_create_running_task_log(
    db: Session,
    queue_task_id: Optional[str],
    task_type: str,
    task_target_id: Optional[int],
    detail_json: Optional[dict[str, Any]] = None,
) -> Optional[TaskLog]:
    """Bind ``queue_task_id`` to an active TaskLog, creating one if needed.

    The consumer-side idempotency seam: every Celery task calls this
    helper immediately after picking up the message. The returned
    row is the one the worker should own for the lifetime of the
    work; ``None`` is the **stale-message** signal — the caller must
    skip the work without committing further writes.

    Returns ``None`` when:

    * ``queue_task_id`` matches an existing row that is already
      terminal (a redelivered broker message after the row was
      finalized by the maintenance heartbeat).
    * The candidate scan and INSERT both lose the race against a
      newer active row that claims the same dedupe-key (PG partial
      unique index ``on_conflict_do_nothing``; SQLite
      ``IntegrityError`` rollback).

    Behaviour is preserved verbatim from the pre-Wave-5 monolith;
    every existing unit / integration test in
    ``tests/unit/test_task_dispatch_binding.py`` continues to pin
    the same outcomes.
    """
    detail_payload = ensure_dict_detail(detail_json)
    detail_payload, dedupe_key = _ensure_dedupe_key(task_type, task_target_id, detail_payload)
    now = datetime.now(timezone.utc)
    lease_seconds = settings.ANALYSIS_LEASE_SECONDS

    if queue_task_id:
        existing = (
            db.query(TaskLog)
            .filter(TaskLog.queue_task_id == queue_task_id, TaskLog.task_type == task_type)
            .order_by(TaskLog.id.desc())
            .first()
        )
        if existing:
            bound = bind_existing_queue_row(
                db,
                existing=existing,
                detail_payload=detail_payload,
                task_type=task_type,
                task_target_id=task_target_id,
                dedupe_key=dedupe_key,
                queue_task_id=queue_task_id,
                now=now,
                lease_seconds=lease_seconds,
            )
            if bound is not None:
                return bound
            # Stale message path: the matched row is terminal, so
            # there is no active TaskLog to claim. Returning ``None``
            # here matches the pre-Wave-5 behaviour pin in
            # ``test_bind_running_log_skips_stale_message_for_timed_out_row``.
            return None

    bound = bind_pending_candidate(
        db,
        detail_payload=detail_payload,
        task_type=task_type,
        task_target_id=task_target_id,
        dedupe_key=dedupe_key,
        queue_task_id=queue_task_id,
        now=now,
        lease_seconds=lease_seconds,
    )
    if bound is not None:
        return bound

    return insert_fresh_running_row(
        db,
        detail_payload=detail_payload,
        task_type=task_type,
        task_target_id=task_target_id,
        dedupe_key=dedupe_key,
        queue_task_id=queue_task_id,
        now=now,
        lease_seconds=lease_seconds,
    )


__all__ = [
    "bind_or_create_running_task_log",
]
