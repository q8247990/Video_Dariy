"""Analysis-pipeline stages.

One module per stage keeps the analyzer readable:

``contracts``
    Tunables, exception hierarchy and the :class:`AnalysisPorts` bundle.
``plan``
    Build the per-file work plan and the deterministic run id.
``claim``
    Bind the task log, take the lease, and mark failures.
``execute``
    Resolve the provider, run one vision call, persist the checkpoint.
``finalize``
    Aggregate results, transition the session, enqueue webhooks.
"""

from __future__ import annotations

from src.services.analysis.claim import (
    ClaimContext,
    SkipOutcome,
    claim_session_for_analysis,
    finalize_skip,
    finalize_stale_message,
    handle_deadlock_retry_exhausted,
    is_deadlock_operational_error,
    mark_session_sealed_for_retry,
    purge_stale_checkpoints,
)
from src.services.analysis.contracts import (
    DEADLOCK_MAX_RETRIES,
    NOT_FOUND_RETRY_DELAYS_SECONDS,
    POSTGRES_RETRYABLE_SQLSTATES,
    RAW_MP4_NUM_FRAMES,
    WEBHOOK_EVENT_SESSION_ANALYZED,
    AnalysisPorts,
    AnalysisStageError,
    DeadlockRetrySignal,
    LateWorkerFencingError,
    RawResponseCapture,
    RecoverySignalError,
    build_analysis_ports,
)
from src.services.analysis.execute import (
    CheckpointWriteResult,
    ClaimGuards,
    SubChunkRunResult,
    build_provider_client,
    build_sub_chunk_extra_body,
    build_sub_chunk_video_url,
    enforce_fencing,
    execute_sub_chunk,
    get_or_create_checkpoint,
    mark_checkpoint_error,
    mark_checkpoint_processing,
    record_recovery_failure,
    write_sub_chunk_checkpoint,
    write_token_usage,
)
from src.services.analysis.finalize import (
    cancel_session,
    completed_results_for_run,
    enroll_session_analyzed_webhooks,
    finalize_session_success,
    find_session_analyzed_subscribers,
    merge_session_fields,
    record_failed_transition,
    record_partial_failure_transition,
    replace_session_events,
)
from src.services.analysis.plan import (
    AnalysisPlan,
    SubChunkPlan,
    analysis_run_id_for_paths,
    build_analysis_plan,
    sub_chunk_fingerprint,
)

__all__ = [
    "AnalysisPlan",
    "AnalysisPorts",
    "AnalysisStageError",
    "ClaimContext",
    "ClaimGuards",
    "CheckpointWriteResult",
    "DEADLOCK_MAX_RETRIES",
    "DeadlockRetrySignal",
    "LateWorkerFencingError",
    "NOT_FOUND_RETRY_DELAYS_SECONDS",
    "POSTGRES_RETRYABLE_SQLSTATES",
    "RAW_MP4_NUM_FRAMES",
    "RawResponseCapture",
    "RecoverySignalError",
    "SkipOutcome",
    "SubChunkPlan",
    "SubChunkRunResult",
    "WEBHOOK_EVENT_SESSION_ANALYZED",
    "analysis_run_id_for_paths",
    "build_analysis_plan",
    "build_analysis_ports",
    "build_provider_client",
    "build_sub_chunk_extra_body",
    "build_sub_chunk_video_url",
    "cancel_session",
    "claim_session_for_analysis",
    "completed_results_for_run",
    "enforce_fencing",
    "enroll_session_analyzed_webhooks",
    "execute_sub_chunk",
    "finalize_session_success",
    "finalize_skip",
    "finalize_stale_message",
    "find_session_analyzed_subscribers",
    "get_or_create_checkpoint",
    "handle_deadlock_retry_exhausted",
    "is_deadlock_operational_error",
    "mark_checkpoint_error",
    "mark_checkpoint_processing",
    "mark_session_sealed_for_retry",
    "merge_session_fields",
    "purge_stale_checkpoints",
    "record_failed_transition",
    "record_partial_failure_transition",
    "record_recovery_failure",
    "replace_session_events",
    "sub_chunk_fingerprint",
    "write_sub_chunk_checkpoint",
    "write_token_usage",
]
