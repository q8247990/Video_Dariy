"""Checkpoint / usage persistence with late-worker fencing guards.

Three write paths live here:

* :func:`get_or_create_checkpoint` — the
  ``SessionAnalysisCheckpoint`` upsert used by the sub-chunk loop.
  Returns the checkpoint row in the right state for the caller to
  decide whether to skip (already success) or process (pending /
  processing / error).
* :func:`_checkpoint_for_work` — the legacy, kwargs-based signature
  used by the existing unit test
  ``test_analyze_session_changed_input_invalidates_stale_checkpoints``
  (tests import it directly from this module).
* :func:`write_sub_chunk_checkpoint` / :func:`write_token_usage` /
  :func:`mark_checkpoint_processing` — the success / token / lease
  writes the sub-chunk loop issues.

The fencing guards used here (in order of strictness):

1. ``analysis_run_id`` — derived from the chunk file identities; if
   the files changed between worker hand-offs, the old worker's
   ``run_id`` will not match the current row.
2. ``input_fingerprint`` — content fingerprint of the system prompt,
   user prompt, video bytes, and sub-chunk geometry. If anything in
   the LLM input differs (e.g. a fresh prompt template), the new
   fingerprint is set in :func:`get_or_create_checkpoint` first.
3. ``lease_owner`` + ``generation`` — taken from the
   :class:`ClaimGuards` and re-checked on every write via
   :func:`enforce_fencing`. These are stored in
   ``TaskLog.detail_json`` so we never have to add a column (no
   migration in this wave).

The failure-path helpers (``enforce_fencing`` /
``mark_checkpoint_error`` / ``record_recovery_failure``) live in
:mod:`.checkpoint_failure` so this file can focus on the happy
path.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.models.llm_provider import LLMProvider
from src.models.session_analysis_checkpoint import SessionAnalysisCheckpoint
from src.services.analysis.chunk_plan import SubChunkPlan, sub_chunk_fingerprint
from src.services.analysis.errors import LateWorkerFencingError
from src.services.llm_qos import record_token_usage
from src.services.task_dispatch_control import renew_task_lease
from src.services.video_analysis.schemas import RecognitionResultDTO

# State machine values for ``SessionAnalysisCheckpoint.state``.
CHECKPOINT_STATE_PENDING = "pending"
CHECKPOINT_STATE_PROCESSING = "processing"
CHECKPOINT_STATE_SUCCESS = "success"
CHECKPOINT_STATE_ERROR = "error"

# In-flight states where a write is allowed by the same worker that
# started the sub-chunk. ``error`` is included so a re-run after a
# prior failure re-uses the same row id (and the same
# ``analysis_run_id`` / ``input_fingerprint``).
RESUMABLE_STATES = frozenset(
    {CHECKPOINT_STATE_PENDING, CHECKPOINT_STATE_PROCESSING, CHECKPOINT_STATE_ERROR}
)


@dataclass(frozen=True)
class ClaimGuards:
    """Fencing guards captured by :func:`claim.claim_session_for_analysis`.

    The :class:`LateWorkerFencingError` is raised when one of these
    values no longer matches the current TaskLog row at write time.

    ``lease_owner`` is ``Optional[str]`` to mirror
    :attr:`src.models.task_log.TaskLog.lease_owner`; a missing lease
    owner at claim time means the row was bound to a heartbeat-derived
    pending task without a Celery queue_task_id. The fencing
    comparison in :func:`enforce_fencing` handles the ``None`` case
    (a stale or absent lease owner always raises
    :class:`LateWorkerFencingError`).
    """

    task_log_id: int
    analysis_run_id: str
    generation: int
    lease_owner: Optional[str]


@dataclass(frozen=True)
class CheckpointWriteResult:
    """Outcome of :func:`write_sub_chunk_checkpoint`."""

    checkpoint: SessionAnalysisCheckpoint
    written: bool  # True if the UPDATE matched a row; False if the worker is late.


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _lookup_checkpoint(
    db: Session,
    *,
    session_id: int,
    analysis_run_id: str,
    chunk_index: int,
    sub_chunk_index: int,
) -> Optional[SessionAnalysisCheckpoint]:
    return (
        db.query(SessionAnalysisCheckpoint)
        .filter(
            SessionAnalysisCheckpoint.session_id == session_id,
            SessionAnalysisCheckpoint.analysis_run_id == analysis_run_id,
            SessionAnalysisCheckpoint.chunk_index == chunk_index,
            SessionAnalysisCheckpoint.sub_chunk_index == sub_chunk_index,
        )
        .first()
    )


def get_or_create_checkpoint(
    db: Session,
    *,
    session_id: int,
    claim: ClaimGuards,
    sub_chunk: SubChunkPlan,
    system_prompt: str,
    user_prompt: str,
    video_data_url: str,
) -> SessionAnalysisCheckpoint:
    """Resolve / upsert the checkpoint row for one sub-chunk.

    Returns the row in one of:

    * ``state=success`` — the caller should skip the LLM call.
    * ``state in {pending, processing, error}`` — the caller should
      claim the row, increment ``attempt_count``, and proceed with
      the LLM call.

    When the row already exists with a different
    ``input_fingerprint`` the previous fields are reset to
    ``pending`` so the new fingerprint is the one persisted in the
    eventual success write.
    """
    fingerprint = sub_chunk_fingerprint(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        video_data_url=video_data_url,
        sub_chunk=sub_chunk,
    )

    checkpoint = _lookup_checkpoint(
        db,
        session_id=session_id,
        analysis_run_id=claim.analysis_run_id,
        chunk_index=sub_chunk.chunk_index,
        sub_chunk_index=sub_chunk.sub_chunk_index,
    )
    if checkpoint is not None:
        if checkpoint.input_fingerprint != fingerprint:
            checkpoint.state = CHECKPOINT_STATE_PENDING
            checkpoint.input_fingerprint = fingerprint
            checkpoint.event_payload = None
            checkpoint.prompt_tokens = 0
            checkpoint.completion_tokens = 0
            checkpoint.total_tokens = 0
            checkpoint.completed_at = None
        return checkpoint

    checkpoint = SessionAnalysisCheckpoint(
        session_id=session_id,
        analysis_run_id=claim.analysis_run_id,
        chunk_index=sub_chunk.chunk_index,
        sub_chunk_index=sub_chunk.sub_chunk_index,
        start_offset_seconds=sub_chunk.start_offset_seconds,
        input_fingerprint=fingerprint,
        state=CHECKPOINT_STATE_PENDING,
    )
    try:
        with db.begin_nested():
            db.add(checkpoint)
            db.flush()
    except IntegrityError:
        checkpoint = _lookup_checkpoint(
            db,
            session_id=session_id,
            analysis_run_id=claim.analysis_run_id,
            chunk_index=sub_chunk.chunk_index,
            sub_chunk_index=sub_chunk.sub_chunk_index,
        )
        if checkpoint is None:
            raise
    return checkpoint


def _checkpoint_for_work(  # noqa: PLR0913 — legacy signature, kept for tests.
    db: Session,
    *,
    session_id: int,
    analysis_run_id: str,
    chunk_index: int,
    sub_chunk: Any,
    input_fingerprint: str,
) -> SessionAnalysisCheckpoint:
    """Backward-compatible wrapper matching the original signature.

    The existing unit test
    ``test_analyze_session_changed_input_invalidates_stale_checkpoints``
    imports ``_checkpoint_for_work`` from this module and exercises
    the input-fingerprint-invalidation branch. The orchestration
    itself calls the cleaner :func:`get_or_create_checkpoint`.
    """
    start_offset_seconds = int(getattr(sub_chunk, "start_offset_seconds", 0))
    sub_chunk_index = int(getattr(sub_chunk, "sub_chunk_index", 0))

    checkpoint = _lookup_checkpoint(
        db,
        session_id=session_id,
        analysis_run_id=analysis_run_id,
        chunk_index=chunk_index,
        sub_chunk_index=sub_chunk_index,
    )
    if checkpoint is not None:
        if checkpoint.input_fingerprint != input_fingerprint:
            checkpoint.state = CHECKPOINT_STATE_PENDING
            checkpoint.input_fingerprint = input_fingerprint
            checkpoint.event_payload = None
            checkpoint.prompt_tokens = 0
            checkpoint.completion_tokens = 0
            checkpoint.total_tokens = 0
            checkpoint.completed_at = None
        return checkpoint

    checkpoint = SessionAnalysisCheckpoint(
        session_id=session_id,
        analysis_run_id=analysis_run_id,
        chunk_index=chunk_index,
        sub_chunk_index=sub_chunk_index,
        start_offset_seconds=start_offset_seconds,
        input_fingerprint=input_fingerprint,
        state=CHECKPOINT_STATE_PENDING,
    )
    try:
        with db.begin_nested():
            db.add(checkpoint)
            db.flush()
    except IntegrityError:
        checkpoint = _lookup_checkpoint(
            db,
            session_id=session_id,
            analysis_run_id=analysis_run_id,
            chunk_index=chunk_index,
            sub_chunk_index=sub_chunk_index,
        )
        if checkpoint is None:
            raise
    return checkpoint


def write_sub_chunk_checkpoint(
    db: Session,
    *,
    claim: ClaimGuards,
    checkpoint: SessionAnalysisCheckpoint,
    recognition_result: RecognitionResultDTO,
    prompt_tokens: int,
    completion_tokens: int,
    total_tokens: int,
) -> CheckpointWriteResult:
    """Persist the success row for one sub-chunk.

    The UPDATE is filtered on ``analysis_run_id`` +
    ``input_fingerprint`` so a stale worker (whose
    ``analysis_run_id`` does not match the row that was looked up
    earlier in the same worker) cannot overwrite a newer worker's
    success row. The fence check is enforced twice — once via
    :func:`checkpoint_failure.enforce_fencing` and once via the SQL
    filter — to guarantee that a race window between the read and
    the write still results in ``rowcount == 0``.

    The function does NOT renew the lease itself — the slim task
    keeps the lease renewal in its own short transaction.
    """
    from src.services.analysis.checkpoint_failure import enforce_fencing

    enforce_fencing(db, claim=claim)

    payload = recognition_result.model_dump(mode="json")
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
                SessionAnalysisCheckpoint.state: CHECKPOINT_STATE_SUCCESS,
                SessionAnalysisCheckpoint.event_payload: payload,
                SessionAnalysisCheckpoint.prompt_tokens: prompt_tokens,
                SessionAnalysisCheckpoint.completion_tokens: completion_tokens,
                SessionAnalysisCheckpoint.total_tokens: total_tokens,
                SessionAnalysisCheckpoint.completed_at: _utc_now(),
            },
            synchronize_session=False,
        )
    )
    if not updated:
        raise LateWorkerFencingError(
            f"Checkpoint {checkpoint.id} for run {claim.analysis_run_id} not writable "
            "(row already finalized by another worker, or fingerprint / run_id mismatch)."
        )

    db.refresh(checkpoint)
    return CheckpointWriteResult(checkpoint=checkpoint, written=True)


def write_token_usage(
    db: Session,
    *,
    provider: LLMProvider,
    checkpoint: SessionAnalysisCheckpoint,
    session_id: int,
    usage: dict[str, int],
    record_token_usage_fn: Any = None,
) -> None:
    """Write the LLMUsageLog row bound to this checkpoint.

    ``record_token_usage_fn`` accepts the
    :func:`src.services.llm_qos.record_token_usage` callable so the
    analyzer orchestration can pass its own reference (patchable in
    the orchestration module's namespace). When ``None`` the helper
    uses the canonical helper from :mod:`src.services.llm_qos`.
    """
    if record_token_usage_fn is None:
        record_token_usage_fn = record_token_usage
    record_token_usage_fn(
        db,
        provider_id=provider.id,
        provider_name_snapshot=provider.provider_name,
        scene="video_analysis",
        usage=usage,
        session_id=session_id,
        analysis_checkpoint_id=checkpoint.id,
    )


def mark_checkpoint_processing(
    db: Session,
    *,
    claim: ClaimGuards,
    checkpoint: SessionAnalysisCheckpoint,
    lease_owner: str,
) -> None:
    """Flip the row to ``processing`` and increment ``attempt_count``.

    Called inside the same short-lived transaction that persists the
    in-flight row — the DB is closed before the LLM HTTP call so the
    worker does not hold a checked-out connection during the
    external request.

    A retry of a previously-failed sub-chunk observes the existing
    row in ``state=error`` (or ``processing`` if the previous run
    crashed mid-LLM call); ``RESUMABLE_STATES`` admits both, so a
    fresh attempt can flip the row back to ``processing``.
    """
    from src.services.analysis.checkpoint_failure import enforce_fencing

    enforce_fencing(db, claim=claim)
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
                SessionAnalysisCheckpoint.state: CHECKPOINT_STATE_PROCESSING,
                SessionAnalysisCheckpoint.attempt_count: SessionAnalysisCheckpoint.attempt_count
                + 1,
            },
            synchronize_session=False,
        )
    )
    if not updated:
        raise LateWorkerFencingError(
            f"Checkpoint {checkpoint.id} already moved past resumable by another worker."
        )
    db.refresh(checkpoint)
    renew_task_lease(db, claim.task_log_id, lease_owner)


__all__ = [
    "CHECKPOINT_STATE_ERROR",
    "CHECKPOINT_STATE_PENDING",
    "CHECKPOINT_STATE_PROCESSING",
    "CHECKPOINT_STATE_SUCCESS",
    "CheckpointWriteResult",
    "ClaimGuards",
    "RESUMABLE_STATES",
    "_checkpoint_for_work",
    "get_or_create_checkpoint",
    "mark_checkpoint_processing",
    "write_sub_chunk_checkpoint",
    "write_token_usage",
]
