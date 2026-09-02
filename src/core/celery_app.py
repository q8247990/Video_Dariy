from celery import Celery

from src.core.config import settings
from src.core.logging_config import set_correlation_id

TASK_MODULES = [
    "src.tasks.session_build",
    "src.tasks.analyzer",
    "src.tasks.summarizer",
    "src.tasks.webhook",
    "src.tasks.task_maintenance",
]

celery_app = Celery(
    "worker",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
    include=TASK_MODULES,
)


def _on_task_prerun(sender: object = None, task_id: str = "", **kwargs: object) -> None:
    del sender, kwargs
    set_correlation_id(task_id or None)


def _on_task_postrun(**kwargs: object) -> None:
    del kwargs
    set_correlation_id(None)


def _wire_correlation_signals() -> None:
    """Thread the outbox ``event_id`` (Celery ``task_id``) into structured logs.

    The publisher always sends the broker message with
    ``task_id = OutboxEvent.event_id`` (ADR §7), so inside the worker the
    ``self.request.id`` equals the outbox correlation key. Connecting a
    ``task_prerun`` / ``task_postrun`` pair sets the :data:`CORRELATION_CONTEXT`
    ContextVar for the duration of each task, so every log line the task
    emits carries ``correlation_id=<event_id>`` via
    :class:`src.core.logging_config.CorrelationFilter`. This connects the
    API dispatch log and the publisher log (which already log the same
    ``event_id``) into a single retrievable chain.

    The handlers are module-level functions so Celery's weakref-backed
    signal receivers stay alive; a locally-scoped closure would be
    garbage-collected and silently disconnect.
    """
    try:
        from celery.signals import task_postrun, task_prerun  # pragma: no cover

        task_prerun.connect(_on_task_prerun)
        task_postrun.connect(_on_task_postrun)
    except Exception:  # noqa: BLE001  (signal wiring must never break boot)
        # Non-Celery import contexts / tests ignore correlation wiring.
        return


_wire_correlation_signals()


celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="Asia/Shanghai",
    enable_utc=True,
    task_track_started=True,
    # Ack only after execution: a lost worker returns the message to its
    # original queue (celery, analysis_hot, or analysis_full) for retry.
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    # Tasks may handle SoftTimeLimitExceeded and persist retryable state before
    # the hard kill. Redis has no native dead-letter queue; exhausted retries
    # remain in TaskLog for the existing maintenance/retry workflow.
    task_soft_time_limit=3300,
    task_time_limit=3600,
    beat_schedule={
        "heartbeat": {
            "task": "src.tasks.task_maintenance.heartbeat",
            "schedule": 60.0,
        },
        "dispatch-scheduled-daily-summary": {
            "task": "src.tasks.summarizer.dispatch_scheduled_daily_summary_task",
            "schedule": 60.0,
        },
    },
)
