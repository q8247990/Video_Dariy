"""Atomic publish use case for daily summaries + notification events.

This module is the seam that turns a *successful*
``daily_summary_generation_attempt`` into three durable, atomic writes:

1. ``daily_summary`` is upserted with the LLM-produced content for the
   date.
2. The attempt is flipped ``→ succeeded`` via
   :class:`DailySummaryAttemptRepository` (Todo 15). The state-machine
   guard is enforced — a final attempt that has already reached a
   terminal state is rejected with
   :class:`AttemptNotInValidStateError` and the business write is
   rolled back.
3. For every webhook subscriber the use case creates its own
   ``TaskLog`` row and enrolls an :class:`OutboxCommand` pointing at
   ``src.tasks.webhook.send_webhook_task`` via :func:`enqueue_command`
   (Todo 12). The outbox publisher (Todo 13) picks the rows up from
   the durable table and publishes to the broker.

All three writes happen in the **caller's** transaction. The caller
owns the commit; a ``rollback()`` between any two writes leaves zero
rows behind, satisfying the atomicity contract from ADR
``docs/adr/0011-transactional-outbox-and-task-lifecycle.md``.

Failure / cancellation paths
============================

The publish use case handles the **success** path. When the summarizer
(Todo 19) decides the attempt has failed or has been cancelled, it
calls :meth:`DailySummaryAttemptRepository.mark_failed` /
``mark_cancelled`` / ``mark_timed_out`` directly. Those terminal
helpers **do not** touch ``daily_summary``; a previous successful
summary for the same date is therefore preserved verbatim — exactly
what the product brief asks for ("失败/取消仅终结 attempt,不物理删除已有成功日报").

Why one OutboxCommand per subscriber (not one fan-out command)
===============================================================

The outbox's partial unique index
``(task_log_id) WHERE status='pending'`` rejects two pending rows for
the same ``task_log_id`` — using a single ``TaskLog`` for the fan-out
would mean a retry / re-publish of the daily summary cannot enqueue a
*new* batch of webhook rows until the original fan-out leaves the
pending state. Giving each subscriber its own ``TaskLog`` keeps the
partial unique from blocking legitimate concurrent webhooks and keeps
each delivery's idempotency story self-contained (the publisher's
``task_id`` is the per-subscriber ``event_id``).

Why the payload embeds ``webhook_id``
=====================================

``src.tasks.webhook.send_webhook_task`` (Todo 19 will own its
behaviour change) currently iterates every enabled
``WebhookConfig`` row and applies the subscription filter, so today a
single command fan-outs. The publish use case already knows which
subscribers match the ``daily_summary_generated`` event, so the
payload embeds ``webhook_id``. Todo 19 will teach the consumer to
honour that field and only deliver to the targeted subscriber;
meanwhile, the redundant fan-out is harmless because each subscriber
only matches once per event anyway. The PG tests assert the
``webhook_id`` is round-tripped in ``kwargs_json``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Optional
from uuid import UUID

from sqlalchemy.orm import Session

from src.application.outbox.contracts import OutboxCommand
from src.application.outbox.enqueue import enqueue_command
from src.application.summary_attempt.repository import DailySummaryAttemptRepository
from src.application.summary_attempt.state_machine import (
    ACTIVE_STATUSES,
    DailySummaryAttemptStatus,
)
from src.application.summary_publication.repository import DailySummaryRepository
from src.models.daily_summary_attempt import DailySummaryGenerationAttempt
from src.services.pipeline_constants import TaskType
from src.services.task_dispatch_control import create_pending_task_log

#: Event type string emitted by the daily-summary webhook payload.
#: Mirrors ``src.services.summarizer.constants.WEBHOOK_EVENT_DAILY_SUMMARY_GENERATED``
#: so the consumer-side subscription filter recognises it; the string
#: is part of the public product contract and must not drift.
DAILY_SUMMARY_WEBHOOK_EVENT_TYPE: str = "daily_summary_generated"

#: Celery task the outbox publishes for each subscriber. Listed in
#: :class:`src.application.outbox.registry.OutboxCommandRegistry`.
_WEBHOOK_TASK_NAME: str = "src.tasks.webhook.send_webhook_task"

#: Default queue for the webhook task. Matches the registry's default.
_DEFAULT_QUEUE: str = "celery"

#: Stable fallback ``overall_summary`` for the empty-event day. The
#: summarizer (Todo 19) is free to override by supplying a non-empty
#: ``overall_summary`` in the command; this constant is what we use
#: when the caller passes ``""``.
EMPTY_DAY_FALLBACK_TEXT: str = "今日无事件,无需特别关注。"


class AttemptNotInValidStateError(Exception):
    """The attempt cannot transition to ``succeeded``.

    Raised by :func:`publish_daily_summary` when the supplied attempt
    row is already in a terminal state (``succeeded`` /
    ``failed`` / ``cancelled`` / ``timed_out`` / ``superseded``). The
    summarizer (Todo 19) treats this as a fatal failure and rolls
    back the surrounding transaction; ``daily_summary`` is **not**
    touched.
    """

    def __init__(self, message: str, *, payload: Optional[dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.payload: dict[str, Any] = dict(payload or {})

    def __repr__(self) -> str:
        return f"AttemptNotInValidStateError({self.payload!r})"


@dataclass(frozen=True)
class PublishDailySummaryCommand:
    """Inputs for the publish use case.

    Attributes:
        summary_date: The date the summary covers (timezone-naive
            ``date`` object).
        attempt_id: The ``DailySummaryGenerationAttempt`` row that
            drove this publication.
        task_log_id: The ``TaskLog`` row bound to the attempt; only
            used to make sure the attempt row points at a valid
            correlation log.
        summary_content_json: Caller-controlled structured payload
            (typically the LLM's parsed response). Recognised keys:

            - ``summary_title`` (``str``): localized title.
            - ``overall_summary`` (``str``): prose body; empty string
              triggers :data:`EMPTY_DAY_FALLBACK_TEXT`.
            - ``subject_sections`` (``list[dict]``): per-subject
              roll-up; stored verbatim.
            - ``attention_items`` (``list[dict]``): operator-facing
              attention list; stored verbatim.
            - ``events_count`` (``int``): ``len(events)`` for the day;
              drives the empty-day fallback.
            - ``provider_id`` (``int | None``): provider FK.
            - ``provider_name_snapshot`` (``str | None``):
              provider's display name at generation time.

        webhook_subscribers: List of ``WebhookConfig.id`` values that
            should receive the ``daily_summary_generated`` event.
            One ``TaskLog`` + ``OutboxEvent`` pair is enrolled per id.
            Pass an empty list to skip webhook emission.
        dry_run: When ``True`` the use case still validates the
            attempt state and computes the upserted ``summary_id``
            (so the caller's tests can assert on it) yet does NOT
            enroll webhook outbox rows. Use this in unit tests that
            want to inspect the would-be payload without standing up
            the full outbox flow.
    """

    summary_date: date
    attempt_id: int
    task_log_id: int
    summary_content_json: dict[str, Any]
    webhook_subscribers: list[int] = field(default_factory=list)
    dry_run: bool = False


@dataclass(frozen=True)
class PublishDailySummaryOutcome:
    """Result of a successful :func:`publish_daily_summary` call.

    Attributes:
        summary_id: The ``daily_summary.id`` that was upserted.
        attempt_status: The attempt row's status after the call
            (``SUCCEEDED`` on the happy path).
        webhook_event_ids: One ``event_id`` per enrolled webhook
            outbox row. Empty when ``cmd.webhook_subscribers`` is
            empty or ``dry_run`` is ``True``. Order matches
            ``cmd.webhook_subscribers``.
    """

    summary_id: int
    attempt_status: DailySummaryAttemptStatus
    webhook_event_ids: list[UUID] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Use case
# ---------------------------------------------------------------------------


def publish_daily_summary(
    db: Session,
    cmd: PublishDailySummaryCommand,
) -> PublishDailySummaryOutcome:
    """Atomically publish a successful daily summary + notification events.

    All three writes happen in the **caller's** transaction. The caller
    MUST commit; a ``rollback()`` between any two writes leaves zero
    rows behind.

    Algorithm
    ---------

    1. Load the attempt row via the attempt repository. Verify its
       status is one of :data:`ACTIVE_STATUSES` (Todo 15). When it is
       not, raise :class:`AttemptNotInValidStateError` so the caller
       rolls back; ``daily_summary`` is **not** touched.
    2. Upsert ``daily_summary`` for ``cmd.summary_date`` via
       :class:`DailySummaryRepository`. The ``id`` of the upserted row
       is the ``summary_id``.
    3. Transition the attempt ``→ succeeded`` via the repository's
       state-machine guard (``mark_succeeded``). The helper stamps
       ``finished_at`` and clears residue ``error_type`` /
       ``last_error`` / ``failure_reason`` columns.
    4. For each subscriber id in ``cmd.webhook_subscribers``:
       ``create_pending_task_log`` + ``enqueue_command`` enroll an
       :class:`OutboxCommand` whose ``kwargs_json`` carries
       ``event_type``, ``webhook_id`` and a JSON-safe ``payload``
       (built from the upserted content).
    5. Return :class:`PublishDailySummaryOutcome`.

    Args:
        db: Caller-owned SQLAlchemy session. The use case never
            commits or rolls back; that is the caller's job.
        cmd: The publish command.

    Returns:
        :class:`PublishDailySummaryOutcome` summarising the upserted
        row, the new attempt status and the enrolled webhook
        ``event_id`` values.

    Raises:
        :class:`AttemptNotInValidStateError`: the attempt is not in a
            state that may transition to ``succeeded`` (terminal
            state, or unknown state). The caller MUST roll back.
        :class:`OutboxRegistryError`: the webhook task name is not in
            the registry whitelist (should not happen in production;
            the constant lives next to the registry entry).
        :class:`OutboxPayloadError`: the JSON-safety rules reject the
            would-be payload (the use case only builds JSON-safe
            values today, but the guard is structural).
        :class:`ValueError`: ``cmd.task_log_id`` is ``None`` /
            inconsistent with the attempt row.
    """
    attempt_repo = DailySummaryAttemptRepository(db)
    summary_repo = DailySummaryRepository(db)

    # Step 1: validate the attempt state. ``_load_attempt_for_publish``
    # raises ``AttemptNotInValidStateError`` when the row is missing or
    # in a state from which ``→ succeeded`` is not legal.
    attempt = _load_attempt_for_publish(attempt_repo, cmd.attempt_id)
    if int(attempt.task_log_id or 0) != int(cmd.task_log_id):
        # The caller's correlation log does not match the attempt's
        # ``task_log_id`` — treat as a programmer error rather than a
        # silent corruption.
        raise ValueError(
            f"attempt {cmd.attempt_id} is bound to task_log_id="
            f"{attempt.task_log_id}, but cmd.task_log_id={cmd.task_log_id}"
        )

    # Step 2: upsert ``daily_summary``. Done before the attempt state
    # transition so the consumer can read the row even if the
    # subsequent steps raise (the caller will roll back, but the row
    # is INSERTed in the same tx and disappears with the rollback).
    summary_id = _upsert_summary(summary_repo, cmd)

    # Step 3: transition the attempt ``→ succeeded``. The helper
    # returns ``None`` only on a TOCTOU race where a parallel
    # finalizer already moved the row to a terminal state; in that
    # case we MUST abort and let the caller roll back the upsert.
    finalised = attempt_repo.mark_succeeded(cmd.attempt_id)
    if finalised is None:
        raise AttemptNotInValidStateError(
            f"attempt {cmd.attempt_id} could not be transitioned to 'succeeded'",
            payload={
                "attempt_id": cmd.attempt_id,
                "summary_date": cmd.summary_date.isoformat(),
            },
        )

    # Step 4: enroll webhook outbox rows. Skip entirely in ``dry_run``
    # mode (the test surface wants the would-be outcome without the
    # broker side effects).
    webhook_event_ids: list[UUID] = []
    if not cmd.dry_run:
        webhook_event_ids = _enroll_webhook_outbox(db, cmd, summary_id)

    return PublishDailySummaryOutcome(
        summary_id=summary_id,
        attempt_status=DailySummaryAttemptStatus(finalised.status),
        webhook_event_ids=webhook_event_ids,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _load_attempt_for_publish(
    attempt_repo: DailySummaryAttemptRepository,
    attempt_id: int,
) -> DailySummaryGenerationAttempt:
    """Return the attempt row, asserting the state is active.

    Raises :class:`AttemptNotInValidStateError` when the row is
    missing or already in a terminal state. The publish use case
    requires the attempt to be in one of :data:`ACTIVE_STATUSES` so
    the state-machine guard in :meth:`DailySummaryAttemptRepository.mark_succeeded`
    will accept the transition.
    """
    from sqlalchemy import select

    from src.models.daily_summary_attempt import DailySummaryGenerationAttempt

    db = attempt_repo._db  # noqa: SLF001  — session owned by the repo
    stmt = (
        select(DailySummaryGenerationAttempt)
        .where(DailySummaryGenerationAttempt.id == attempt_id)
        .execution_options(populate_existing=True)
    )
    attempt = db.execute(stmt).scalars().first()
    if attempt is None:
        raise AttemptNotInValidStateError(
            f"attempt {attempt_id} does not exist",
            payload={"attempt_id": attempt_id},
        )
    if attempt.status not in {status.value for status in ACTIVE_STATUSES}:
        raise AttemptNotInValidStateError(
            f"attempt {attempt_id} is in state '{attempt.status}', "
            f"which cannot transition to 'succeeded'",
            payload={
                "attempt_id": attempt_id,
                "current_status": attempt.status,
                "expected_statuses": sorted(s.value for s in ACTIVE_STATUSES),
            },
        )
    return attempt


def _upsert_summary(
    summary_repo: DailySummaryRepository,
    cmd: PublishDailySummaryCommand,
) -> int:
    """Map the command's JSON payload onto the model columns and upsert."""
    content = cmd.summary_content_json
    overall = str(content.get("overall_summary") or "")
    if cmd.summary_content_json.get("events_count", 0) == 0 and not overall:
        # Empty-event day: use the stable fallback text. The
        # summarizer can override by supplying a non-empty
        # ``overall_summary``.
        overall = EMPTY_DAY_FALLBACK_TEXT
    return summary_repo.upsert_for_date(
        summary_date=cmd.summary_date,
        summary_title=str(content.get("summary_title") or ""),
        overall_summary=overall,
        subject_sections_json=content.get("subject_sections"),
        attention_items_json=content.get("attention_items"),
        event_count=int(content.get("events_count") or 0),
        provider_id=content.get("provider_id"),
        provider_name_snapshot=content.get("provider_name_snapshot"),
    )


def _enroll_webhook_outbox(
    db: Session,
    cmd: PublishDailySummaryCommand,
    summary_id: int,
) -> list[UUID]:
    """Create a pending ``TaskLog`` + outbox row per subscriber.

    Each subscriber gets its own ``TaskLog`` row so the outbox's
    partial unique on ``(task_log_id) WHERE status='pending'`` does
    not block legitimate concurrent webhooks (one per subscriber).
    The returned ``event_id`` list mirrors ``cmd.webhook_subscribers``
    one-to-one.
    """
    enrolled: list[UUID] = []
    timestamp = datetime.now(tz=timezone.utc).isoformat()
    for webhook_id in cmd.webhook_subscribers:
        task_log, _created = create_pending_task_log(
            db,
            task_type=TaskType.WEBHOOK_PUSH,
            task_target_id=int(webhook_id),
            detail_json={
                "event_type": DAILY_SUMMARY_WEBHOOK_EVENT_TYPE,
                "webhook_id": int(webhook_id),
                "summary_date": cmd.summary_date.isoformat(),
                "summary_id": summary_id,
            },
        )
        payload = _build_webhook_payload(
            cmd=cmd,
            summary_id=summary_id,
            webhook_id=int(webhook_id),
            timestamp=timestamp,
        )
        command = OutboxCommand(
            task_name=_WEBHOOK_TASK_NAME,
            queue=_DEFAULT_QUEUE,
            args=(),
            kwargs={
                "event_type": DAILY_SUMMARY_WEBHOOK_EVENT_TYPE,
                "webhook_id": int(webhook_id),
                "payload": payload,
            },
        )
        outcome = enqueue_command(db, command, task_log)
        enrolled.append(outcome.event.event_id)
    return enrolled


def _build_webhook_payload(
    *,
    cmd: PublishDailySummaryCommand,
    summary_id: int,
    webhook_id: int,
    timestamp: str,
) -> dict[str, Any]:
    """Build the JSON-safe webhook payload dict.

    The shape mirrors :func:`src.services.webhook_payload.build_webhook_event_payload`
    — ``event`` + ``version`` + ``generated_at`` + ``data`` — so the
    consumer-side :func:`is_webhook_event_payload` guard accepts it.
    """
    content = cmd.summary_content_json
    data: dict[str, Any] = {
        "summary_date": cmd.summary_date.isoformat(),
        "summary_id": summary_id,
        "events_count": int(content.get("events_count") or 0),
        "webhook_id": webhook_id,
    }
    title = content.get("summary_title")
    if title:
        data["summary_title"] = str(title)
    overall = content.get("overall_summary")
    if overall:
        data["overall_summary"] = str(overall)
    subject_sections = content.get("subject_sections")
    if subject_sections is not None:
        data["subject_sections"] = subject_sections
    attention_items = content.get("attention_items")
    if attention_items is not None:
        data["attention_items"] = attention_items
    return {
        "event": DAILY_SUMMARY_WEBHOOK_EVENT_TYPE,
        "version": "1.0",
        "generated_at": timestamp,
        "data": data,
    }


__all__ = [
    "DAILY_SUMMARY_WEBHOOK_EVENT_TYPE",
    "EMPTY_DAY_FALLBACK_TEXT",
    "AttemptNotInValidStateError",
    "PublishDailySummaryCommand",
    "PublishDailySummaryOutcome",
    "publish_daily_summary",
]
