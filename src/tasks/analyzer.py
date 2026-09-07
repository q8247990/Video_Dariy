"""Session analysis Celery task: thin wrapper over the orchestration module.

Wave 5 split the previous monolith (~780 LOC, mixed lease / LLM /
checkpoint / finalize responsibilities) into a Celery task plus stage
modules under :mod:`src.services.analysis`. Wave 5b then moved the
pipeline body (claim + plan + sub-chunk loop + finalize + failure
handling) out of this task entry into
:mod:`src.tasks._analyzer_orchestration`. This task entry now only:

* builds the ``queue_task_id`` from the Celery request;
* delegates to :func:`run_session_analysis` in the orchestration
  module, which owns the whole pipeline (including its DB session).

This module deliberately exposes **only** ``analyze_session_task``:
the Celery registry name (``src.tasks.analyzer.analyze_session_task``)
is the stable dispatch contract referenced by the outbox registry and
the celery dispatcher. Pipeline helpers live in
:mod:`src.tasks._analyzer_orchestration` and are called directly from
there — tests that need to stub a helper monkeypatch the name in that
module's namespace.
"""

from __future__ import annotations

from typing import Any

from src.core.celery_app import celery_app
from src.services.analysis.constants import DEADLOCK_MAX_RETRIES
from src.tasks._analyzer_orchestration import run_session_analysis


@celery_app.task(bind=True, max_retries=DEADLOCK_MAX_RETRIES)  # type: ignore[untyped-decorator]
def analyze_session_task(self: Any, session_id: int, priority: str = "hot") -> dict[str, Any]:
    """Analyze a sealed session using LLM vision.

    Dispatched to ``analysis_hot`` or ``analysis_full`` and consumed
    by the dedicated ``vision-worker`` Celery process
    (``-Q analysis_hot,analysis_full --concurrency=1``, supervised
    inside the backend container); the worker process runs at most
    one session analysis task at a time. The fencing guards in the
    analysis stages are the new defence against two workers
    overlapping on the same ``(session, queue_task_id)``.
    """
    queue_task_id = str(getattr(getattr(self, "request", None), "id", "") or "")
    return run_session_analysis(
        self=self,
        session_id=session_id,
        priority=priority,
        queue_task_id=queue_task_id,
    )


__all__ = ["analyze_session_task"]
