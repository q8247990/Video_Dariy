"""Daily-summary Celery tasks - thin wrapper over the orchestration module.

Owns the two Celery task decorators and the ``task_db_session``
envelope; the dispatch guard / claim + evidence / LLM phase / publish
+ finalize logic lives in :mod:`src.tasks._summarizer_orchestration`.
``generate_daily_summary_task`` binds the ``TaskLog`` and handles the
top-level commit and ``TaskCancellationRequested`` envelope; the
patched seam names (``_get_pipeline_orchestrator``, ``home_now``,
``SERIAL_SPLIT_PROMPT_THRESHOLD``, ``celery_app``, ``TaskLog``) are
re-exported below so ``monkeypatch.setattr(\"src.tasks.summarizer.X\",
...)`` keeps landing (read dynamically by the orchestration via its
seam).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Optional

from src.application.pipeline.orchestrator import PipelineOrchestrator
from src.core.celery_app import celery_app
from src.db.session import task_db_session
from src.models.daily_summary import DailySummary
from src.models.task_log import TaskLog
from src.services.home_timezone import home_now as home_now
from src.services.onboarding import DEFAULT_DAILY_SUMMARY_SCHEDULE
from src.services.pipeline_constants import TaskType
from src.services.summarizer import (  # noqa: F401 - re-exported for legacy test contract
    SERIAL_SPLIT_PROMPT_THRESHOLD,
    WEBHOOK_EVENT_DAILY_SUMMARY_GENERATED,
    get_home_timezone,
)
from src.services.task_dispatch_control import (
    TaskCancellationRequested,
    bind_or_create_running_task_log,
    finalize_cancelled_task_log,
    get_task_log_for_update,
)
from src.tasks._container import get_container
from src.tasks._summarizer_orchestration import (
    GenerationOutcome,  # noqa: F401 - re-exported for legacy test contract
    run_daily_summary_generation,
    run_dispatch_scheduled,
)

logger = logging.getLogger(__name__)


def _get_pipeline_orchestrator() -> PipelineOrchestrator:
    """Build a fresh :class:`PipelineOrchestrator` from the composition root.

    Re-exported for the legacy test contract; the orchestration reads
    this dynamically through its seam so the
    ``monkeypatch.setattr(\"src.tasks.summarizer._get_pipeline_orchestrator\",
    ...)`` tests keep landing.
    """
    return PipelineOrchestrator(dispatcher=get_container().dispatcher)


@celery_app.task(bind=True)  # type: ignore[untyped-decorator]
def dispatch_scheduled_daily_summary_task(self: Any) -> dict[str, Any]:
    """Celery-beat entry point: dispatch today's daily-summary task if due."""
    with task_db_session() as db:
        now = home_now(get_home_timezone(db))
        return run_dispatch_scheduled(db, container=get_container(), now=now)


@celery_app.task(bind=True)  # type: ignore[untyped-decorator]
def generate_daily_summary_task(self: Any, target_date_str: Optional[str] = None) -> dict[str, Any]:
    """Celery-worker entry point: generate one day's daily summary.

    Args:
        target_date_str: ISO ``YYYY-MM-DD`` date. ``None`` defaults to
            "yesterday" in the configured home timezone.

    Returns:
        ``{"summary_date": ..., "event_count": ...}`` on success;
        ``{"skipped": True, "reason": ...}`` /
        ``{"cancelled": True, ...}`` on the non-success paths.
    """
    queue_task_id = str(getattr(getattr(self, "request", None), "id", "") or "")
    container = get_container()

    with task_db_session() as db:
        if target_date_str:
            target_date = datetime.strptime(target_date_str, "%Y-%m-%d").date()
        else:
            zone = get_home_timezone(db)
            target_date = home_now(zone).date() - timedelta(days=1)

        task_log = bind_or_create_running_task_log(
            db,
            queue_task_id=queue_task_id or None,
            task_type=TaskType.DAILY_SUMMARY_GENERATION,
            task_target_id=None,
            detail_json={"target_date": str(target_date)},
        )
        if task_log is None:
            logger.warning(
                "Stale daily summary message %s for %s; task log already finalized, skipping",
                queue_task_id,
                target_date,
            )
            return {"skipped": True, "reason": "stale_message"}
        db.commit()

        try:
            response = run_daily_summary_generation(
                db=db,
                target_date=target_date,
                queue_task_id=queue_task_id,
                container=container,
                task_log=task_log,
            )
        except TaskCancellationRequested as exc:
            logger.info("Daily summary task cancelled for %s", target_date)
            db.rollback()
            db.query(DailySummary).filter(DailySummary.summary_date == target_date).delete()
            refreshed = get_task_log_for_update(db, task_log.id)
            if refreshed is not None:
                finalize_cancelled_task_log(
                    refreshed,
                    str(exc),
                    {"target_date": str(target_date), "cancelled": True},
                )
            db.commit()
            return {"cancelled": True, "summary_date": str(target_date)}

        db.commit()
        return response


__all__ = [
    "DEFAULT_DAILY_SUMMARY_SCHEDULE",
    "GenerationOutcome",
    "SERIAL_SPLIT_PROMPT_THRESHOLD",
    "TaskLog",
    "dispatch_scheduled_daily_summary_task",
    "generate_daily_summary_task",
    "home_now",
    "WEBHOOK_EVENT_DAILY_SUMMARY_GENERATED",
]
