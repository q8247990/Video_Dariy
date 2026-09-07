"""Analysis-pipeline stage modules.

The analyzer Celery task is a thin orchestrator that delegates to
the modules in this package. Each module owns one execution stage:

* :mod:`.claim` — bind a TaskLog, take the lease, transition the
  session to ``ANALYZING``.
* :mod:`.chunk_plan` — split a session into ``SessionVideoChunk`` /
  ``SubChunk`` work units and compute the deterministic
  ``analysis_run_id``.
* :mod:`.provider` — resolve the default vision provider and build
  a fresh :class:`LLMGatewayPort`.
* :mod:`.sub_chunk_runner` — execute one sub-chunk (no DB).
* :mod:`.checkpoint_writer` — persist the success row with
  late-worker fencing guards.
* :mod:`.aggregator` — fold per-sub-chunk results into session
  fields and rebuild ``EventRecord`` rows.
* :mod:`.finalize` — transition ``VideoSession`` + ``TaskLog`` to
  their terminal state.
* :mod:`.recovery` — classify PG serialization faults and translate
  them into Celery ``self.retry`` signals.
* :mod:`.constants` — tunables shared across the stages.
* :mod:`.errors` — exception hierarchy.

The stages are intentionally side-effect-free except for the
persistence layer (``checkpoint_writer`` / ``finalize``), and the
external-call stage (``sub_chunk_runner``) never touches SQLAlchemy
— the LLM / ffmpeg work happens without a checked-out DB
connection. Prompt assembly is intentionally owned by the Celery
orchestrator in :mod:`src.tasks._analyzer_orchestration` because it
imports the ``src.application.prompt`` contracts / compiler; the
``src.services.analysis`` layer is forbidden from reaching up into
``src.application.*``.
"""

from __future__ import annotations

from src.services.analysis.checkpoint_failure import (
    enforce_fencing,
    mark_checkpoint_error,
    record_recovery_failure,
)
from src.services.analysis.checkpoint_writer import (
    CHECKPOINT_STATE_ERROR,
    CHECKPOINT_STATE_PENDING,
    CHECKPOINT_STATE_PROCESSING,
    CHECKPOINT_STATE_SUCCESS,
    CheckpointWriteResult,
    ClaimGuards,
    get_or_create_checkpoint,
    mark_checkpoint_processing,
    write_sub_chunk_checkpoint,
    write_token_usage,
)
from src.services.analysis.chunk_plan import (
    ChunkPlan,
    SubChunkPlan,
    build_chunk_plan,
    sub_chunk_fingerprint,
)
from src.services.analysis.claim import (
    ClaimContext,
    SkipOutcome,
    claim_session_for_analysis,
    finalize_skip,
    finalize_stale_message,  # noqa: F401 — re-exported for the orchestrator
)
from src.services.analysis.constants import RAW_MP4_NUM_FRAMES
from src.services.analysis.errors import (
    AnalysisStageError,
    DeadlockRetrySignal,
    LateWorkerFencingError,
    RawResponseCapture,
    RecoverySignalError,
)
from src.services.analysis.failure import (
    handle_deadlock_retry_exhausted,  # noqa: F401 — re-exported for completeness
)
from src.services.analysis.finalize import (
    cancel_session,
    finalize_session_success,
    record_failed_transition,
    record_partial_failure_transition,
)
from src.services.analysis.provider import build_provider_client
from src.services.analysis.recovery import is_deadlock_operational_error
from src.services.analysis.sub_chunk_runner import (
    SubChunkRunResult,
    build_sub_chunk_extra_body,
    build_sub_chunk_video_url,
    execute_sub_chunk,
)

__all__ = [
    "AnalysisStageError",
    "CHECKPOINT_STATE_ERROR",
    "CHECKPOINT_STATE_PENDING",
    "CHECKPOINT_STATE_PROCESSING",
    "CHECKPOINT_STATE_SUCCESS",
    "ChunkPlan",
    "ClaimContext",
    "ClaimGuards",
    "CheckpointWriteResult",
    "DeadlockRetrySignal",
    "LateWorkerFencingError",
    "RAW_MP4_NUM_FRAMES",
    "RawResponseCapture",
    "RecoverySignalError",
    "SkipOutcome",
    "SubChunkPlan",
    "SubChunkRunResult",
    "build_chunk_plan",
    "build_provider_client",
    "build_sub_chunk_extra_body",
    "build_sub_chunk_video_url",
    "cancel_session",
    "claim_session_for_analysis",
    "enforce_fencing",
    "execute_sub_chunk",
    "finalize_session_success",
    "finalize_skip",
    "finalize_stale_message",
    "get_or_create_checkpoint",
    "is_deadlock_operational_error",
    "mark_checkpoint_error",
    "mark_checkpoint_processing",
    "record_failed_transition",
    "record_partial_failure_transition",
    "record_recovery_failure",
    "sub_chunk_fingerprint",
    "write_sub_chunk_checkpoint",
    "write_token_usage",
]
