"""Analysis-pipeline contracts: tunables, exception hierarchy and port bundle."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

# --- constants.py ---
RAW_MP4_NUM_FRAMES: int = 120


# --- constants.py ---
WEBHOOK_EVENT_SESSION_ANALYZED: str = "session_analyzed"


# --- constants.py ---
DEADLOCK_MAX_RETRIES: int = 3


# --- constants.py ---
NOT_FOUND_RETRY_DELAYS_SECONDS: tuple[float, ...] = (0.5, 1.0, 2.0)


# --- constants.py ---
POSTGRES_RETRYABLE_SQLSTATES: frozenset[str] = frozenset({"40P01", "40001"})


# --- errors.py ---
class AnalysisStageError(Exception):
    """Base class for the analysis-stage module tree."""


# --- errors.py ---
class LateWorkerFencingError(AnalysisStageError):
    """The worker's claim has been invalidated by a recovery take-over.

    Raised by the checkpoint / finalize / aggregate stages when one of
    the fencing guards (run_id, generation, lease_owner, input
    fingerprint) does not match the value captured by :func:`claim`.
    Callers must treat this as a no-op: the new worker owns the lease
    and the late worker is expected to exit cleanly without writing.
    """


# --- errors.py ---
class DeadlockRetrySignal(AnalysisStageError):
    """Internal: the analyzer hit a retryable PG serialization fault.

    The Celery task catches this and calls ``self.retry`` with
    exponential backoff. The string carries the original ``pgcode``
    for the operator log.
    """

    def __init__(self, original: BaseException) -> None:
        super().__init__(str(original))
        self.original = original


# --- errors.py ---
class RecoverySignalError(AnalysisStageError):
    """Internal: the analyzer exhausted its retry budget.

    The Celery task catches this, transitions the TaskLog to FAILED,
    and either marks the session as FAILED (no checkpoints written) or
    PARTIAL (some checkpoints written). The original exception is
    preserved on ``self.original``.
    """

    def __init__(self, original: BaseException) -> None:
        super().__init__(str(original))
        self.original = original


# --- errors.py ---
class RawResponseCapture(AnalysisStageError):
    """Internal: a parser / post-LLM stage failed but we captured the
    raw gateway response for the failure log.

    The orchestrator unwraps :attr:`original` to raise the real
    exception after copying :attr:`raw_response_text` into the
    failure-log payload. The exception itself is never observed by
    Celery or the unit tests — it is purely a side-channel between
    the sub-chunk runner and the orchestrator's exception handler.
    """

    def __init__(self, original: BaseException, raw_response_text: str | None) -> None:
        super().__init__(str(original))
        self.original = original
        self.raw_response_text = raw_response_text


# --- ports.py ---
if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from src.models.event_record import EventRecord
    from src.models.llm_provider import LLMProvider
    from src.services.analysis.plan import AnalysisPlan
    from src.services.frame_sampling.budget import FramePlan
    from src.services.frame_sampling.types import FrameSet


# --- ports.py ---
PlanAnalysisFn = Callable[["Session", int], "AnalysisPlan"]


# --- ports.py ---
VideoDataUrlFn = Callable[[str], str]


# --- ports.py ---
ReplaceSessionEventsFn = Callable[["Session", int, "list[EventRecord]"], int]


# --- ports.py ---
FrameSetFn = Callable[["LLMProvider", str, "FramePlan"], "FrameSet"]


# --- ports.py ---
@dataclass(frozen=True)
class AnalysisPorts:
    """Frozen bundle of the helpers the analyzer orchestration depends on.

    The dataclass is frozen so a fake instance is also immutable (the
    production wiring must not drift under the feet of a running worker).
    """

    plan_analysis: PlanAnalysisFn
    video_data_url: VideoDataUrlFn
    replace_session_events: ReplaceSessionEventsFn
    frame_set: FrameSetFn


# --- ports.py ---
def build_analysis_ports(*, frame_set: FrameSetFn) -> AnalysisPorts:
    """Return the production :class:`AnalysisPorts` bundle.

    ``frame_set`` is an explicit required keyword: services must not
    import ``src.infrastructure``, so the frame-sampling adapter binding
    is supplied by the composition root
    (``src.application.bootstrap.build_analysis_ports_bundle``).

    The remaining bindings are imported lazily so the analyzer task's
    module load-time graph does not change.
    """
    from src.services.analysis.finalize import _replace_session_events
    from src.services.analysis.plan import build_analysis_plan
    from src.services.session_analysis_video import build_video_data_url

    return AnalysisPorts(
        plan_analysis=build_analysis_plan,
        video_data_url=build_video_data_url,
        replace_session_events=_replace_session_events,
        frame_set=frame_set,
    )


__all__ = [
    "AnalysisPorts",
    "AnalysisStageError",
    "DEADLOCK_MAX_RETRIES",
    "DeadlockRetrySignal",
    "LateWorkerFencingError",
    "NOT_FOUND_RETRY_DELAYS_SECONDS",
    "POSTGRES_RETRYABLE_SQLSTATES",
    "RAW_MP4_NUM_FRAMES",
    "RawResponseCapture",
    "RecoverySignalError",
    "WEBHOOK_EVENT_SESSION_ANALYZED",
    "build_analysis_ports",
]
