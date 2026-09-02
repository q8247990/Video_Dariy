"""Checkpoint failure paths and the fencing enforcement helper.

Split out of :mod:`.checkpoint_writer` so the happy-path file can
stay focused on the upsert / success / token writes; this module
owns the parts that translate exceptions into persisted
``state=error`` rows and the
:class:`~src.services.analysis.errors.LateWorkerFencingError` raise
points.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from src.models.session_analysis_checkpoint import SessionAnalysisCheckpoint
from src.models.task_log import TaskLog
from src.services.analysis.checkpoint_writer import RESUMABLE_STATES, ClaimGuards
from src.services.analysis.errors import LateWorkerFencingError
from src.services.llm_output_utils import truncate_text
from src.services.pipeline_constants import TaskStatus
from src.services.pipeline_state import transition_task_log
from src.services.task_dispatch_control import finalize_task_log

logger = logging.getLogger(__name__)


def enforce_fencing(
    db: Session,
    *,
    claim: ClaimGuards,
) -> None:
    """Verify the claim still matches the TaskLog at write time.

    The TaskLog row is the single source of truth for lease
    ownership / generation: the heartbeat task increments
    ``recovery_attempt`` (which is mirrored into
    ``detail_json["generation"]``) when a lease expires, so a
    stale worker whose ``ClaimGuards.generation`` is no longer
    equal to the current ``detail_json["generation"]`` is rejected.

    Raises:
        LateWorkerFencingError: when one of the fencing guards
            does not match. The caller must NOT write any data.
    """
    row = db.query(TaskLog).filter(TaskLog.id == claim.task_log_id).one_or_none()
    if row is None:
        raise LateWorkerFencingError(
            f"TaskLog {claim.task_log_id} disappeared during analysis write"
        )
    detail = row.detail_json if isinstance(row.detail_json, dict) else {}
    current_generation = detail.get("generation")
    current_lease = row.lease_owner
    if current_generation != claim.generation or current_lease != claim.lease_owner:
        raise LateWorkerFencingError(
            "Stale analysis claim: "
            f"task_log={claim.task_log_id} "
            f"generation={current_generation} (expected {claim.generation}), "
            f"lease_owner={current_lease!r} (expected {claim.lease_owner!r})"
        )


def mark_checkpoint_error(
    db: Session,
    *,
    claim: ClaimGuards,
    checkpoint: SessionAnalysisCheckpoint,
    error: BaseException,
) -> None:
    """Persist the ``state=error`` row before re-raising to the caller.

    The fencing guard is evaluated; if it fails the row is left
    untouched (the new worker owns the lease and will overwrite the
    error on its own take-over run).
    """
    try:
        enforce_fencing(db, claim=claim)
    except LateWorkerFencingError:
        logger.info(
            "Skipping checkpoint error write for session_id=%s chunk=%s-%s: lease taken over",
            checkpoint.session_id,
            checkpoint.chunk_index,
            checkpoint.sub_chunk_index,
        )
        return

    message = truncate_text(str(error), 500) or str(error)
    updated = (
        db.query(SessionAnalysisCheckpoint)
        .filter(
            SessionAnalysisCheckpoint.id == checkpoint.id,
            SessionAnalysisCheckpoint.analysis_run_id == claim.analysis_run_id,
            SessionAnalysisCheckpoint.input_fingerprint == checkpoint.input_fingerprint,
            SessionAnalysisCheckpoint.state.in_(list(RESUMABLE_STATES)),
        )
        .update(
            {
                SessionAnalysisCheckpoint.state: "error",
                SessionAnalysisCheckpoint.last_error: message,
                SessionAnalysisCheckpoint.error_type: type(error).__name__,
            },
            synchronize_session=False,
        )
    )
    if not updated:
        logger.info(
            "Could not write error state for checkpoint %s; row already finalized.",
            checkpoint.id,
        )
        return
    db.refresh(checkpoint)


def record_recovery_failure(  # noqa: PLR0913 — failure detail mirrors the legacy log shape.
    db: Session,
    *,
    claim: ClaimGuards,
    task_log: Any,
    error: BaseException,
    failed_chunk_index: int | None,
    failed_sub_chunk_index: int | None,
    last_prompt_text: str | None,
    last_response_text: str | None,
    last_raw_response_text: str | None,
    priority: str,
    session_id: int | None,
) -> None:
    """Mark the TaskLog as FAILED with the standard analyzer error detail."""
    transition_task_log(
        db,
        claim.task_log_id,
        TaskStatus.RUNNING,
        TaskStatus.FAILED,
        reason="analysis_failed",
        source="analyze_session_task",
    )
    finalize_task_log(
        task_log,
        TaskStatus.FAILED,
        truncate_text(str(error), 500) or str(error),
        {
            "session_id": session_id,
            "failed_chunk_index": failed_chunk_index,
            "failed_sub_chunk_index": failed_sub_chunk_index,
            "error_type": type(error).__name__,
            "prompt_text": truncate_text(last_prompt_text, 4000),
            "raw_response_excerpt": truncate_text(last_response_text, 1500),
            "raw_response_full": truncate_text(last_response_text, 16000),
            "raw_llm_response_excerpt": truncate_text(last_raw_response_text, 1500),
            "raw_llm_response_full": truncate_text(last_raw_response_text, 16000),
            "priority": priority,
        },
    )


__all__ = [
    "enforce_fencing",
    "mark_checkpoint_error",
    "record_recovery_failure",
]
