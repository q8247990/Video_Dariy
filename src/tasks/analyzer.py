"""Session analysis Celery task: thin wrapper over the orchestration module.

Wave 5 split the previous monolith (~780 LOC, mixed lease / LLM /
checkpoint / finalize responsibilities) into a Celery task plus stage
modules under :mod:`src.services.analysis`. Wave 5b then moved the
pipeline body (claim + plan + sub-chunk loop + finalize + failure
handling) out of this task entry into
:mod:`src.tasks._analyzer_orchestration`. This task entry now only:

* opens a ``task_db_session`` for the orchestration lifetime;
* delegates to :func:`run_session_analysis` in the orchestration
  module, which owns the whole pipeline.

The task module remains the monkey-patchable *seam* the existing test
suite relies on: the orchestration reads every patchable helper
(``task_db_session``, ``build_session_video_chunks``,
``build_chunk_sub_chunks``, ``build_home_context``,
``_build_provider_client``, ``_replace_session_events``,
``ensure_task_not_cancelled``, ``_build_prompts``,
``session_chunk_from_sub_chunk``, ``build_chunk_video_data_url``,
``enforce_token_quota``, ``parse_video_recognition_output``,
``record_token_usage``) dynamically off this module at call time, so a
``monkeypatch.setattr("src.tasks.analyzer.X", ...)`` in a test takes
effect unchanged. Every such name is therefore re-exported below.
"""

from __future__ import annotations

from typing import Any

from src.core.celery_app import celery_app
from src.db.session import task_db_session
from src.services.analysis import (
    RAW_MP4_NUM_FRAMES,  # noqa: F401 — re-exported for legacy test contract
)
from src.services.analysis.aggregator import (
    _replace_session_events,  # noqa: F401 — re-exported for legacy test contract
)
from src.services.analysis.checkpoint_writer import (
    _checkpoint_for_work,  # noqa: F401 — re-exported for legacy test contract
)
from src.services.analysis.claim import (  # noqa: E402,F401 — re-exported for legacy tests
    _claim_session_for_analysis,
)
from src.services.analysis.constants import DEADLOCK_MAX_RETRIES
from src.services.analysis.provider import (
    _build_provider_client,  # noqa: F401 — re-exported for legacy test contract
)
from src.services.dispatch.lease import (
    renew_task_lease,  # noqa: F401 — re-exported for legacy test contract
)
from src.services.home_profile import build_home_context
from src.services.llm_output_utils import truncate_text
from src.services.llm_qos import (  # noqa: F401 — re-exported for legacy test contract
    enforce_token_quota,
    record_token_usage,
)
from src.services.session_analysis_video import (
    build_chunk_sub_chunks,  # noqa: F401 — re-exported for legacy test contract
    build_chunk_video_data_url,  # noqa: F401 — re-exported for legacy test contract
    build_session_video_chunks,  # noqa: F401 — re-exported for legacy test contract
    session_chunk_from_sub_chunk,  # noqa: F401 — re-exported for legacy test contract
)
from src.services.task_dispatch_control import (
    ensure_task_not_cancelled,  # noqa: F401 — re-exported for legacy test contract
)
from src.services.video_analysis.output_parser import (
    parse_video_recognition_output,  # noqa: F401 — re-exported for legacy test contract
)
from src.tasks._analyzer_orchestration import (
    _build_prompts,  # noqa: F401 — re-exported for legacy test contract
    run_session_analysis,
)


@celery_app.task(bind=True, max_retries=DEADLOCK_MAX_RETRIES)  # type: ignore[untyped-decorator]
def analyze_session_task(self: Any, session_id: int, priority: str = "hot") -> dict[str, Any]:
    """Analyze a sealed session using LLM vision.

    Dispatched to ``analysis_hot`` or ``analysis_full`` and consumed
    by the dedicated ``celery_vision_worker --concurrency=1``; the
    worker process runs at most one session analysis task at a
    time. The fencing guards in the analysis stages are the new
    defence against two workers overlapping on the same
    ``(session, queue_task_id)``.
    """
    queue_task_id = str(getattr(getattr(self, "request", None), "id", "") or "")
    with task_db_session() as db:
        return run_session_analysis(
            db,
            self=self,
            session_id=session_id,
            priority=priority,
            queue_task_id=queue_task_id,
        )


__all__ = [
    "RAW_MP4_NUM_FRAMES",
    "analyze_session_task",
    "build_chunk_sub_chunks",
    "build_chunk_video_data_url",
    "build_home_context",
    "build_session_video_chunks",
    "enforce_token_quota",
    "ensure_task_not_cancelled",
    "parse_video_recognition_output",
    "record_token_usage",
    "renew_task_lease",
    "session_chunk_from_sub_chunk",
    "task_db_session",
    "truncate_text",
    "_build_prompts",
    "_build_provider_client",
    "_checkpoint_for_work",
    "_claim_session_for_analysis",
    "_replace_session_events",
]
