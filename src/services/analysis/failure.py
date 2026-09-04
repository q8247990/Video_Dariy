"""Deadlock-retry failure handlers for the analysis pipeline.

Called by :mod:`src.tasks._analyzer_orchestration` (which owns the
loop) when the deadlock retry budget is exhausted. The helper opens
its own short DB transaction so the outer loop's session is never
entangled with the failure cleanup.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from src.services.analysis.checkpoint_failure import record_recovery_failure
from src.services.analysis.claim import ClaimContext


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


def mark_session_sealed_for_retry(db: Session, session_id: int) -> None:
    """Roll a session back from ANALYZING to SEALED on a deadlock retry."""
    from src.services.analysis.claim import mark_session_sealed_for_retry as _impl

    _impl(db, session_id)


__all__ = [
    "handle_deadlock_retry_exhausted",
    "mark_session_sealed_for_retry",
]
