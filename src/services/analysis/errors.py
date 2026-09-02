"""Exceptions raised by the analysis stages.

These are *signal* errors the slim Celery task translates into state
machine transitions or Celery ``self.retry`` calls. They never leak
out of ``src/tasks/analyzer.py`` as unhandled exceptions — the task
catches each one, writes the appropriate TaskLog / Session status, and
either re-raises a Celery-friendly error or returns a structured dict.
"""

from __future__ import annotations


class AnalysisStageError(Exception):
    """Base class for the analysis-stage module tree."""


class LateWorkerFencingError(AnalysisStageError):
    """The worker's claim has been invalidated by a recovery take-over.

    Raised by the checkpoint / finalize / aggregate stages when one of
    the fencing guards (run_id, generation, lease_owner, input
    fingerprint) does not match the value captured by :func:`claim`.
    Callers must treat this as a no-op: the new worker owns the lease
    and the late worker is expected to exit cleanly without writing.
    """


class DeadlockRetrySignal(AnalysisStageError):
    """Internal: the analyzer hit a retryable PG serialization fault.

    The Celery task catches this and calls ``self.retry`` with
    exponential backoff. The string carries the original ``pgcode``
    for the operator log.
    """

    def __init__(self, original: BaseException) -> None:
        super().__init__(str(original))
        self.original = original


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


__all__ = [
    "AnalysisStageError",
    "DeadlockRetrySignal",
    "LateWorkerFencingError",
    "RawResponseCapture",
    "RecoverySignalError",
]
