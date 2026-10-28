from typing import Protocol, runtime_checkable

from sqlalchemy.orm import Session

from src.application.pipeline.commands import (
    AnalyzeSessionCommand,
    GenerateDailySummaryCommand,
    SendWebhookCommand,
    SessionBuildCommand,
)


@runtime_checkable
class TaskDispatcherPort(Protocol):
    """Async task dispatcher port.

    Every dispatch method **MUST** be called with a caller-owned
    SQLAlchemy ``Session``. The dispatcher writes the ``TaskLog`` row (with
    the broker ``task_id`` pre-assigned to ``queue_task_id``) and a
    :class:`~src.models.task_delivery.TaskDelivery` intent row in that
    session; the caller commits. The caller then flushes that intent to the
    broker through :func:`~src.services.dispatch.outbox.flush_task_deliveries`.

    The returned ``str | None`` is the broker ``task_id`` (UUID
    stringified) so the consumer-side ``bind_or_create_running_task_log``
    idempotency seam can bind by it.
    """

    def dispatch_session_build(self, db: Session, command: SessionBuildCommand) -> str | None: ...

    def dispatch_analyze_session(
        self, db: Session, command: AnalyzeSessionCommand
    ) -> str | None: ...

    def dispatch_generate_daily_summary(
        self, db: Session, command: GenerateDailySummaryCommand
    ) -> str | None: ...

    def dispatch_webhook(self, db: Session, command: SendWebhookCommand) -> str | None: ...
