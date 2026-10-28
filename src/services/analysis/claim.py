"""Analysis claim stage: bind the task, take the lease, mark failures."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from src.core.pipeline_constants import (
    SessionAnalysisStatus,
    TaskStatus,
    TaskType,
)
from src.models.session_analysis_checkpoint import SessionAnalysisCheckpoint
from src.models.task_log import TaskLog
from src.models.video_session import VideoSession
from src.models.video_source import VideoSource
from src.services.analysis.contracts import (
    NOT_FOUND_RETRY_DELAYS_SECONDS,
    POSTGRES_RETRYABLE_SQLSTATES,
)
from src.services.analysis.execute import ClaimGuards, record_recovery_failure
from src.services.dispatch import (
    bind_or_create_running_task_log,
    finalize_task_log,
)
from src.services.pipeline_state import transition_session, transition_task_log

# --- claim.py ---
logger = logging.getLogger(__name__)


# --- claim.py ---
@dataclass(frozen=True)
class ClaimContext:
    """Returned by :func:`claim_session_for_analysis` on a successful claim.

    Carries the fencing guards used by every subsequent write
    (checkpoint / event_record / session update).

    ``lease_owner`` mirrors :attr:`TaskLog.lease_owner` and is typed as
    ``str | None`` for the same reason: a manually-bound pending row
    (e.g. from the heartbeat without a Celery queue_task_id) can have
    ``lease_owner=None``. In production the analyzer task always
    carries a queue_task_id, so this is normally a non-empty string.
    """

    session: VideoSession
    source: VideoSource
    task_log: TaskLog
    analysis_run_id: str
    generation: int
    lease_owner: str | None

    def to_guards(self) -> ClaimGuards:
        return ClaimGuards(
            task_log_id=self.task_log.id,
            analysis_run_id=self.analysis_run_id,
            generation=self.generation,
            lease_owner=self.lease_owner,
        )


# --- claim.py ---
@dataclass(frozen=True)
class SkipOutcome:
    """Returned when the claim is refused and the task should be SKIPPED."""

    reason: str  # "already_analyzing" / "session_open" / "not_found" / "stale_message"
    detail_message: str
    current_status: str | None = None


# --- claim.py ---
def purge_stale_checkpoints(db: Session, *, session_id: int, keep_run_id: str) -> int:
    """Delete checkpoints from previous analysis runs of one session.

    A new ``analysis_run_id`` is issued whenever the file set or the
    planning scheme changes, leaving the prior run's checkpoints behind.
    They are never read again (``completed_results_for_run`` filters by
    run id) and the token-usage FK is ``ON DELETE SET NULL``, so
    dropping them is safe and keeps the table (and the ``/metrics``
    progress counts) from growing on every re-analysis. Called inside
    the claim transaction, so it races with no other writer.
    """
    return (
        db.query(SessionAnalysisCheckpoint)
        .filter(
            SessionAnalysisCheckpoint.session_id == session_id,
            SessionAnalysisCheckpoint.analysis_run_id != keep_run_id,
        )
        .delete(synchronize_session=False)
    )


# --- claim.py ---
def _claim_session_for_analysis(
    db: Session, session_id: int, analysis_run_id: str
) -> tuple[VideoSession | None, str | None]:
    """Acquire ``ANALYZING`` ownership of a sealed / partial session.

    Mirrors the contract: keep trying the SEALED →
    ANALYZING and PARTIAL → ANALYZING transitions until one
    succeeds; a session already in ANALYZING is reported as
    ``already_analyzing`` so the worker skips cleanly.
    """
    attempts: tuple[float, ...] = (0.0,) + NOT_FOUND_RETRY_DELAYS_SECONDS
    for index, delay in enumerate(attempts):
        updated = any(
            transition_session(
                db,
                session_id,
                from_status,
                SessionAnalysisStatus.ANALYZING,
                reason="analysis_claim",
                source="claim_session_for_analysis",
            ).applied
            for from_status in (
                SessionAnalysisStatus.SEALED,
                SessionAnalysisStatus.PARTIAL,
            )
        )
        if updated:
            purge_stale_checkpoints(db, session_id=session_id, keep_run_id=analysis_run_id)
        db.commit()
        if updated:
            session = db.query(VideoSession).filter(VideoSession.id == session_id).first()
            return session, None

        session = db.query(VideoSession).filter(VideoSession.id == session_id).first()
        if session is not None:
            if session.analysis_status == SessionAnalysisStatus.ANALYZING:
                return None, "already_analyzing"
            if session.analysis_status == SessionAnalysisStatus.OPEN:
                return None, "session_open"
            return None, f"status_{session.analysis_status}"

        if index == len(attempts) - 1:
            break

        time.sleep(delay)
        db.rollback()

    return None, "not_found"


# --- claim.py ---
def _resolve_skip_message(reason: str, session_id: int) -> str:
    if reason == "not_found":
        return f"Skipped session {session_id}, not found"
    if reason == "already_analyzing":
        return f"Skipped session {session_id}, already analyzing"
    if reason == "session_open":
        return f"Skipped session {session_id}, session still open"
    if reason.startswith("status_"):
        return f"Skipped session {session_id}, current status is {reason.removeprefix('status_')}"
    return f"Skipped session {session_id}, current status is {reason}"


# --- claim.py ---
def claim_session_for_analysis(
    db: Session,
    *,
    session_id: int,
    priority: str,
    queue_task_id: str | None,
    analysis_run_id: str,
) -> tuple[ClaimContext | None, SkipOutcome | None]:
    """Bind a TaskLog, take the lease, transition the session to ANALYZING.

    Returns either a :class:`ClaimContext` (success) or a
    :class:`SkipOutcome` (the slim task should finalize the TaskLog
    as SKIPPED and return). A ``None`` tuple with ``None`` outcome
    means the queue message itself is stale (terminal TaskLog row
    already exists for this queue_task_id).
    """
    task_log = bind_or_create_running_task_log(
        db,
        queue_task_id=queue_task_id or None,
        task_type=TaskType.SESSION_ANALYSIS,
        task_target_id=session_id,
        detail_json={"priority": priority},
    )
    if task_log is None:
        logger.warning(
            "Stale analysis message %s for session %s; task log already finalized, skipping",
            queue_task_id,
            session_id,
        )
        return None, None

    db.commit()
    session, skip_reason = _claim_session_for_analysis(db, session_id, analysis_run_id)
    if skip_reason is not None or session is None:
        detail = _resolve_skip_message(skip_reason or "not_found", session_id)
        stripped = (skip_reason or "").removeprefix("status_")
        current_status = stripped if (skip_reason or "").startswith("status_") else None
        return None, SkipOutcome(
            reason=skip_reason or "not_found",
            detail_message=detail,
            current_status=current_status,
        )

    source = db.query(VideoSource).filter(VideoSource.id == session.source_id).first()
    if source is None:
        raise ValueError(f"Video source {session.source_id} not found")

    return _build_claim_context(
        db, session=session, source=source, task_log=task_log, analysis_run_id=analysis_run_id
    )


# --- claim.py ---
def _build_claim_context(
    db: Session,
    *,
    session: VideoSession,
    source: VideoSource,
    task_log: TaskLog,
    analysis_run_id: str,
) -> tuple[ClaimContext, None]:
    """Mirror ``recovery_attempt`` into ``detail_json["generation"]``.

    Doing it here (rather than relying on the heartbeat to bump it
    later) means a worker that crashes *after* a fresh re-claim
    already has a generation one greater than the previous claim.
    The detail_json mutation is committed immediately so subsequent
    calls to :func:`enforce_fencing` see the freshly-stamped
    generation / analysis_run_id values.
    """
    detail = task_log.detail_json if isinstance(task_log.detail_json, dict) else {}
    generation = int(detail.get("generation") or task_log.recovery_attempt)
    detail["generation"] = generation
    detail["analysis_run_id"] = analysis_run_id
    task_log.detail_json = detail
    flag_modified(task_log, "detail_json")
    db.commit()
    lease_owner = task_log.lease_owner
    return ClaimContext(
        session=session,
        source=source,
        task_log=task_log,
        analysis_run_id=analysis_run_id,
        generation=generation,
        lease_owner=lease_owner,
    ), None


# --- claim.py ---
def finalize_skip(
    db: Session,
    *,
    task_log: TaskLog,
    session_id: int,
    outcome: SkipOutcome,
    priority: str,
) -> dict[str, Any]:
    """Mark the TaskLog as SKIPPED and return the slim task's response dict."""
    detail = {
        "session_id": session_id,
        "skipped": True,
        "reason": outcome.reason,
        "priority": priority,
    }
    if outcome.current_status is not None:
        detail["current_status"] = outcome.current_status

    transition_task_log(
        db,
        task_log.id,
        TaskStatus.RUNNING,
        TaskStatus.SKIPPED,
        reason="analysis_skipped",
        source="skip_analysis_task",
    )
    finalize_task_log(db, task_log, TaskStatus.SKIPPED, outcome.detail_message, detail)
    db.commit()
    return {"events_created": 0, "skipped": True, "reason": outcome.reason}


# --- claim.py ---
def finalize_stale_message() -> dict[str, Any]:
    """Return the dict emitted when the queue message itself is stale."""
    return {"skipped": True, "reason": "stale_message"}


# --- claim.py ---
def mark_session_sealed_for_retry(db: Session, session_id: int) -> None:
    """Roll a session back from ANALYZING to SEALED on a deadlock retry."""
    session = db.query(VideoSession).filter(VideoSession.id == session_id).first()
    if session is None:
        return
    if session.analysis_status == SessionAnalysisStatus.ANALYZING:
        transition_session(
            db,
            session.id,
            SessionAnalysisStatus.ANALYZING,
            SessionAnalysisStatus.SEALED,
            reason="deadlock_retry",
            source="mark_session_sealed_for_retry",
        )


# --- claim.py ---
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


# --- claim.py ---
def is_deadlock_operational_error(exc: BaseException) -> bool:
    """Return True if ``exc`` is a PG deadlock / serialization fault."""
    if not isinstance(exc, OperationalError):
        return False
    original = getattr(exc, "orig", None)
    if original is None:
        return False

    pgcode = str(getattr(original, "pgcode", "") or "")
    if pgcode in POSTGRES_RETRYABLE_SQLSTATES:
        return True

    message = str(original).lower()
    return "deadlock detected" in message or "could not serialize access" in message


__all__ = [
    "ClaimContext",
    "SkipOutcome",
    "claim_session_for_analysis",
    "finalize_skip",
    "finalize_stale_message",
    "handle_deadlock_retry_exhausted",
    "is_deadlock_operational_error",
    "mark_session_sealed_for_retry",
    "purge_stale_checkpoints",
]
