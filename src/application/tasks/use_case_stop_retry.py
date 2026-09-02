"""Stop / retry use cases for asynchronous task lifecycle endpoints.

These functions encapsulate the orchestration that used to live directly
in :mod:`src.api.v1.endpoints.tasks`:

* ``stop_task_log_use_case`` looks up the ``TaskLog`` row, asks the
  composition-root :class:`~src.application.ports.task_control.TaskControlPort`
  to revoke the Celery ``task_id`` (``terminate=True``), and updates the
  row's status / cancellation flag in the caller-supplied ``Session``.
  Revoke errors are caught and surfaced as a ``5001`` business error so
  the HTTP layer maps them to ``502`` without leaking infrastructure
  details.

* ``retry_task_log_use_case`` delegates to
  :func:`src.services.task_retry.retry_task` while binding the
  ``PipelineOrchestrator`` to the ``TaskDispatcherPort`` from the
  composition root — that keeps the service-layer helper untouched
  (Wave 5 scope) while removing the need for the endpoint to import
  :mod:`src.infrastructure.tasks.celery_dispatcher` directly.

The use cases own **no** SQLAlchemy session: the caller passes the
request-scoped ``Session`` and is responsible for committing it on
success. This matches the pre-existing ``src/api`` pattern (endpoint
functions take a ``db: Session`` FastAPI dependency) and avoids the use
case holding long-lived ORM state.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from src.application.bootstrap import Container
from src.application.pipeline.orchestrator import PipelineOrchestrator
from src.core.i18n import t
from src.models.task_log import TaskLog
from src.services.pipeline_constants import TaskStatus
from src.services.task_retry import retry_task

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StopTaskLogResult:
    """Result of :func:`stop_task_log_use_case`.

    ``error_code`` follows the project's business error convention
    (``0`` on success; ``4002`` / ``4004`` / ``5001`` on failure). The
    ``payload`` field carries the ``task_log_id`` / ``status`` /
    ``cancel_requested`` triple that the legacy endpoint returned, so
    callers can construct identical HTTP response bodies.
    """

    error_code: int
    error_message: str
    payload: Optional[dict[str, object]] = None


@dataclass(frozen=True)
class RetryTaskLogResult:
    """Result of :func:`retry_task_log_use_case`.

    On success ``task_id`` carries the Celery task identifier and
    ``error_code`` is ``0``. On failure ``error_code`` is the business
    code returned by :func:`src.services.task_retry.retry_task` and
    ``task_id`` (when set) is the duplicate task's identifier, matching
    the legacy endpoint response.
    """

    error_code: int
    error_message: str
    task_id: Optional[str] = None


def stop_task_log_use_case(
    *,
    db: Session,
    task_log_id: int,
    locale: str,
    container: Container,
) -> StopTaskLogResult:
    """Revoke a queued / running task and update its ``TaskLog``.

    Behaviour matches the legacy :func:`src.api.v1.endpoints.tasks.stop_task_log`
    endpoint exactly:

    * ``404`` → business code ``4002`` (``task.log_not_found``)
    * Row not in ``{PENDING, RUNNING}`` → ``4004`` (``task.only_active_can_stop``)
    * Revoke failure → ``5001`` (``task.stop_failed``) and no status mutation
    * Otherwise: ``cancel_requested = True``; ``PENDING`` rows flip to
      ``CANCELLED`` with a finished_at timestamp; ``RUNNING`` rows keep
      their status and receive the ``task.cancel_requested`` message.
    """

    row = db.query(TaskLog).filter(TaskLog.id == task_log_id).first()
    if row is None:
        return StopTaskLogResult(error_code=4002, error_message=t("task.log_not_found", locale))

    if row.status not in {TaskStatus.PENDING, TaskStatus.RUNNING}:
        return StopTaskLogResult(
            error_code=4004,
            error_message=t("task.only_active_can_stop", locale),
        )

    if row.queue_task_id:
        try:
            container.task_control.revoke(row.queue_task_id, terminate=True)
        except Exception as exc:  # noqa: BLE001 — surface broker errors as 5001
            logger.exception("Failed to revoke task_id=%s", row.queue_task_id)
            return StopTaskLogResult(
                error_code=5001,
                error_message=t("task.stop_failed", locale, error=exc),
            )

    row.cancel_requested = True
    if row.status == TaskStatus.PENDING:
        row.status = TaskStatus.CANCELLED
        row.message = t("task.cancelled_before_exec", locale)
        row.finished_at = datetime.now(timezone.utc)
    else:
        row.message = t("task.cancel_requested", locale)

    payload = {
        "task_log_id": row.id,
        "status": row.status,
        "cancel_requested": row.cancel_requested,
    }
    return StopTaskLogResult(error_code=0, error_message="", payload=payload)


def retry_task_log_use_case(
    *,
    db: Session,
    task_log_id: int,
    locale: str,
    container: Container,
) -> RetryTaskLogResult:
    """Retry a failed/timeout/cancelled task via the dispatcher port.

    OperationalError raised by the underlying broker is converted into
    the existing ``5001`` business code (``task.queue_unavailable``) to
    preserve the legacy HTTP status mapping (``502``). All other
    validation outcomes are produced by :func:`src.services.task_retry.retry_task`
    and returned verbatim.
    """

    from kombu.exceptions import OperationalError  # local import — keeps use case hermetic in tests

    row = db.query(TaskLog).filter(TaskLog.id == task_log_id).first()
    if row is None:
        return RetryTaskLogResult(error_code=4002, error_message=t("task.log_not_found", locale))

    orchestrator = PipelineOrchestrator(dispatcher=container.dispatcher)
    try:
        result = retry_task(db, row, orchestrator)
    except OperationalError as exc:
        logger.exception("Failed to retry task log id=%s", task_log_id)
        return RetryTaskLogResult(
            error_code=5001,
            error_message=t("task.queue_unavailable", locale, error=exc),
        )

    if not result.success:
        return RetryTaskLogResult(
            error_code=result.error_code,
            error_message=result.error_message,
            task_id=result.task_id,
        )

    return RetryTaskLogResult(
        error_code=0,
        error_message="",
        task_id=result.task_id,
    )


__all__ = [
    "RetryTaskLogResult",
    "StopTaskLogResult",
    "retry_task_log_use_case",
    "stop_task_log_use_case",
]
