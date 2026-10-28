"""Protocol for the daily-summary attempt repository.

The summarizer stages need the attempt repository's transitions but must
not import the application implementation. This Protocol is the shared
type seam; the concrete repository in
:mod:`src.application.summary_attempt.repository` satisfies it
structurally.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Protocol

from src.models.daily_summary_attempt import DailySummaryGenerationAttempt


@dataclass(frozen=True)
class ClaimedAttempt:
    """Result of claiming the per-date active attempt slot."""

    attempt: DailySummaryGenerationAttempt
    created: bool


class AttemptRepositoryPort(Protocol):
    def next_attempt_no(self, summary_date: date) -> int: ...

    def claim(
        self,
        *,
        summary_date: date,
        attempt_no: int,
        triggered_by: str | None,
        task_log_id: int | None,
    ) -> ClaimedAttempt: ...

    def find_active_for_date(self, summary_date: date) -> DailySummaryGenerationAttempt | None: ...

    def mark_running(
        self,
        attempt_id: int,
        *,
        events_count: int | None = None,
        input_token_estimate: int | None = None,
    ) -> DailySummaryGenerationAttempt | None: ...

    def mark_failed(
        self,
        attempt_id: int,
        *,
        error_type: str,
        last_error: str,
        failure_reason: str,
    ) -> DailySummaryGenerationAttempt | None: ...

    def mark_cancelled(
        self,
        attempt_id: int,
        *,
        last_error: str,
    ) -> DailySummaryGenerationAttempt | None: ...


__all__ = ["AttemptRepositoryPort", "ClaimedAttempt"]
