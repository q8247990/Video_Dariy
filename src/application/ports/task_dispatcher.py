from typing import Optional, Protocol, runtime_checkable

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
    SQLAlchemy ``Session``. The dispatcher writes the ``TaskLog`` and the
    ``OutboxEvent`` in that session in a **single transaction**; the
    caller commits. The dispatcher never opens its own ``SessionLocal``
    and never calls ``celery_app.send_task`` directly — the standalone
    outbox publisher (Todo 13) is the only broker round-trip in the
    codebase.

    The returned ``Optional[str]`` is the ``OutboxEvent.event_id`` (UUID
    stringified); the dispatcher sets ``TaskLog.queue_task_id`` to the
    same value so the consumer-side ``bind_or_create_running_task_log``
    idempotency seam (ADR §7) can bind by ``event_id``.
    """

    def dispatch_session_build(
        self, db: Session, command: SessionBuildCommand
    ) -> Optional[str]: ...

    def dispatch_analyze_session(
        self, db: Session, command: AnalyzeSessionCommand
    ) -> Optional[str]: ...

    def dispatch_generate_daily_summary(
        self, db: Session, command: GenerateDailySummaryCommand
    ) -> Optional[str]: ...

    def dispatch_webhook(self, db: Session, command: SendWebhookCommand) -> Optional[str]: ...
