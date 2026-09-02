"""Outbox enqueue use case.

Thin orchestration layer over :class:`OutboxRepository` that use cases
call to publish a Celery command. The use case is the **only** public
entry point that:

- validates the :class:`OutboxCommand` against the registry whitelist
  and the payload JSON-safety rules from ADR §5 / §6 (delegated to
  :func:`emit_event` inside :meth:`OutboxRepository.enroll_with_task_log`);
- binds the outbox row to the caller-supplied ``TaskLog`` so the
  business write and the broker-publish intent commit atomically;
- returns the :class:`OutboxEvent` whose ``event_id`` the caller can
  hand to the consumer-side bind helper (the broker ``task_id`` will
  match this value per ADR §7).

The use case **does not** call ``send_task``; the publisher process
(Todo 13) owns that. Atomicity is the seam between this Todo and the
cutover (Todo 14): sealing a ``VideoSession`` and dispatching the
analyzer are now one transaction, not two.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

from src.application.outbox.contracts import OutboxCommand
from src.application.outbox.repository import EnrollOutcome, OutboxRepository
from src.models.task_log import TaskLog


def enqueue_command(
    db: Session,
    command: OutboxCommand,
    task_log: TaskLog,
    *,
    dedupe_key: Optional[str] = None,
) -> EnrollOutcome:
    """Persist ``command`` as an outbox row bound to ``task_log``.

    Runs entirely inside the caller's transaction: the row is added
    to ``db`` but not committed. The caller MUST ``commit()`` after the
    surrounding business write (typically ``TaskLog`` creation /
    state transition) so both rows become visible atomically; a
    ``rollback()`` leaves zero rows behind, satisfying ADR §1's
    "atomicity contract".

    Args:
        db: Caller-owned SQLAlchemy session.
        command: The :class:`OutboxCommand` describing the Celery
            task to publish.
        task_log: The ``TaskLog`` row this command is bound to.
            ``task_log.id`` MUST be populated; the use case will raise
            ``ValueError`` otherwise.
        dedupe_key: Optional mirror of ``task_log.dedupe_key``;
            defaults to ``task_log.dedupe_key``.

    Returns:
        :class:`EnrollOutcome`. ``event.event_id`` is the value the
        publisher will use as the Celery ``task_id``; the consumer-side
        bind helper uses the same value as the idempotency key.

    Raises:
        :class:`OutboxRegistryError`: ``command.task_name`` is not in
            the registry whitelist.
        :class:`OutboxPayloadError`: ``command.args`` or
            ``command.kwargs`` violates the JSON-safety rules.
        :class:`OutboxContractViolation`: any other contract guard
            from the DTO layer.
        :class:`ValueError`: ``task_log.id`` is ``None``.
    """
    repo = OutboxRepository(db)
    return repo.enroll_with_task_log(task_log, command, dedupe_key=dedupe_key)


__all__ = ["enqueue_command"]
