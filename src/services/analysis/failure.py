"""Permanent-failure handlers for the analysis pipeline.

Called by :mod:`src.services.analysis.orchestration` (which owns
the loop) when an exception escapes the per-sub-chunk try / except
chain. Both helpers open their own short DB transaction so the
outer loop's session is never entangled with the failure cleanup.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from src.models.session_analysis_checkpoint import SessionAnalysisCheckpoint
from src.services.analysis.checkpoint_failure import (
    mark_checkpoint_error,
    record_recovery_failure,
)
from src.services.analysis.checkpoint_writer import ClaimGuards
from src.services.analysis.claim import ClaimContext
from src.services.analysis.finalize import (
    record_failed_transition,
    record_partial_failure_transition,
)


def handle_permanent_failure(
    db: Session,
    *,
    exc: BaseException,
    claim: ClaimContext,
    guards: ClaimGuards,
    session_id: int,
    priority: str,
) -> None:
    """Translate an escaped exception into FAILED / PARTIAL bookkeeping."""
    current_checkpoint = (
        db.query(SessionAnalysisCheckpoint)
        .filter(
            SessionAnalysisCheckpoint.session_id == session_id,
            SessionAnalysisCheckpoint.analysis_run_id == guards.analysis_run_id,
            SessionAnalysisCheckpoint.state == "processing",
        )
        .order_by(SessionAnalysisCheckpoint.id.desc())
        .first()
    )
    if current_checkpoint is not None:
        mark_checkpoint_error(db, claim=guards, checkpoint=current_checkpoint, error=exc)
    record_recovery_failure(
        db,
        claim=guards,
        task_log=claim.task_log,
        error=exc,
        failed_chunk_index=None,
        failed_sub_chunk_index=None,
        last_prompt_text=None,
        last_response_text=None,
        last_raw_response_text=None,
        priority=priority,
        session_id=session_id,
    )
    if current_checkpoint is not None:
        record_partial_failure_transition(db, session=claim.session, task_log=claim.task_log)
    else:
        record_failed_transition(db, session=claim.session, task_log=claim.task_log)
    db.commit()


def handle_deadlock_retry_exhausted(
    db: Session,
    *,
    exc: BaseException,
    claim: ClaimContext,
    session_id: int,
) -> None:
    """Mark the run as FAILED once the deadlock retry budget is exhausted."""
    record_recovery_failure(
        db,
        claim=claim.to_guards(),
        task_log=claim.task_log,
        error=exc,
        failed_chunk_index=None,
        failed_sub_chunk_index=None,
        last_prompt_text=None,
        last_response_text=None,
        last_raw_response_text=None,
        priority="hot",
        session_id=session_id,
    )
    db.commit()


def handle_deadlock_retry(
    db: Session,
    *,
    claim: Any,
    session_id: int,
    attempt: int,
    max_retries: int,
) -> None:
    """Prepare the task log for the next Celery ``self.retry`` round."""
    del claim
    mark_session_sealed_for_retry(db, session_id)
    from src.services.analysis.checkpoint_failure import record_recovery_failure

    record_recovery_failure(
        db,
        claim=ClaimGuards(
            task_log_id=0,
            analysis_run_id="",
            generation=0,
            lease_owner="",
        ),
        task_log=None,
        error=RuntimeError(f"Deadlock retry {attempt + 1}/{max_retries} scheduled"),
        failed_chunk_index=None,
        failed_sub_chunk_index=None,
        last_prompt_text=None,
        last_response_text=None,
        last_raw_response_text=None,
        priority="hot",
        session_id=session_id,
    )
    db.commit()


def mark_session_sealed_for_retry(db: Session, session_id: int) -> None:
    """Roll a session back from ANALYZING to SEALED on a deadlock retry."""
    from src.services.analysis.claim import mark_session_sealed_for_retry as _impl

    _impl(db, session_id)


__all__ = [
    "handle_deadlock_retry",
    "handle_deadlock_retry_exhausted",
    "handle_permanent_failure",
    "mark_session_sealed_for_retry",
]
