"""Analyzer orchestration - the session-analysis pipeline body.

The Celery task in :mod:`src.tasks.analyzer` is a thin wrapper that
opens a :func:`task_db_session` and delegates the entire
orchestration (claim + plan + sub-chunk loop + finalize + failure
handling) to this module so the task entry stays a ~100-line delegate.

Every helper this module calls is either:

* a real, top-level import called directly (e.g.
  :func:`build_home_context`, :func:`ensure_task_not_cancelled`,
  :func:`enforce_token_quota`, :func:`record_token_usage`,
  :func:`parse_video_recognition_output`), or
* reached through the :class:`~src.services.analysis.ports.AnalysisPorts`
  bundle — the five media-layout / event-replace helpers. Tests inject a
  scripted :class:`AnalysisPorts` via the task-layer holder
  (:func:`src.tasks._container.set_analysis_ports_for_tests`) or via
  the ``ports`` kwarg on :func:`run_session_analysis`, so the
  orchestrator no longer needs monkey-patchable names for those five
  helpers.
"""

from __future__ import annotations

import logging
from typing import Any

from src.application.prompt.compiler import compile_video_recognition_prompt
from src.application.prompt.contracts import (
    SessionPromptContext,
    StrategyPromptContext,
    VideoRecognitionPromptInput,
    VideoSourcePromptContext,
)
from src.db.session import task_db_session
from src.models.session_analysis_checkpoint import SessionAnalysisCheckpoint
from src.models.task_log import TaskLog
from src.models.video_source import VideoSource
from src.services.analysis import (
    RAW_MP4_NUM_FRAMES,
    ClaimGuards,
    DeadlockRetrySignal,
    LateWorkerFencingError,
    build_sub_chunk_extra_body,
    cancel_session,
    claim_session_for_analysis,
    finalize_session_success,
    finalize_skip,
    finalize_stale_message,
    get_or_create_checkpoint,
    is_deadlock_operational_error,
    mark_checkpoint_error,
    mark_checkpoint_processing,
    record_recovery_failure,
    write_sub_chunk_checkpoint,
    write_token_usage,
)
from src.services.analysis.chunk_plan import SubChunkPlan
from src.services.analysis.constants import DEADLOCK_MAX_RETRIES
from src.services.analysis.errors import RawResponseCapture
from src.services.analysis.failure import handle_deadlock_retry_exhausted
from src.services.analysis.finalize import (
    record_failed_transition,
    record_partial_failure_transition,
)
from src.services.analysis.ports import AnalysisPorts
from src.services.analysis.provider import _build_provider_client
from src.services.analysis.sub_chunk_runner import execute_sub_chunk
from src.services.dispatch.cancel import TaskCancellationRequested
from src.services.dispatch.lease import renew_task_lease
from src.services.home_profile import build_home_context
from src.services.llm_output_utils import truncate_text
from src.services.llm_qos import enforce_token_quota, record_token_usage
from src.services.pipeline_constants import SourceType
from src.services.prompt_builder.v2.video_recognition import build_strategy_note
from src.services.task_dispatch_control import ensure_task_not_cancelled
from src.services.video_analysis.enums import VIDEO_EVENT_TYPES
from src.services.video_analysis.output_parser import parse_video_recognition_output

logger = logging.getLogger(__name__)


def _resolve_ingest_type(source: VideoSource) -> str:
    if source.source_type == SourceType.LOCAL_DIRECTORY:
        return "xiaomi_nas_backup"
    return source.source_type


def _build_prompts(
    source: VideoSource,
    home_context: dict[str, Any],
    session: Any,
    sub_chunk: SubChunkPlan,
) -> tuple[str, str]:
    """Assemble the recognition prompts for one sub-chunk work item.

    Lives in the task layer (and not in ``src.services.analysis``)
    because the assembly calls
    ``src.application.prompt.compiler.compile_video_recognition_prompt``
    — a contract/compiler pair the service layer is not allowed to
    reach. The Celery task layer has free application access.
    """
    ingest_type = _resolve_ingest_type(source)
    strategy_note = build_strategy_note(ingest_type=ingest_type, source_type=source.source_type)
    return compile_video_recognition_prompt(
        VideoRecognitionPromptInput(
            home_context=home_context,
            video_source=VideoSourcePromptContext(
                source_name=source.source_name,
                camera_name=source.camera_name,
                location_name=source.location_name,
                prompt_text=source.prompt_text,
                source_type=source.source_type,
            ),
            session_context=SessionPromptContext(
                session_id=session.id,
                source_id=session.source_id,
                session_start_time=session.session_start_time,
                session_end_time=session.session_end_time,
                total_duration_seconds=session.total_duration_seconds,
                segment_index=sub_chunk.sub_chunk_index,
                segment_start_offset_sec=sub_chunk.start_offset_seconds,
                segment_duration_seconds=sub_chunk.duration_seconds,
            ),
            strategy_context=StrategyPromptContext(
                ingest_type=ingest_type,
                source_type=source.source_type,
                strategy_note=strategy_note,
            ),
            event_type_list=sorted(VIDEO_EVENT_TYPES),
        )
    )


def run_session_analysis(
    *,
    self: Any,
    session_id: int,
    priority: str,
    queue_task_id: str,
    ports: AnalysisPorts | None = None,
) -> dict[str, Any]:
    """Claim, plan, run the sub-chunk loop, finalize; return the response dict.

    Owns the whole pipeline lifetime, including the orchestration DB
    session (``task_db_session``); the Celery task wrapper only builds
    ``queue_task_id`` and calls this entry point.

    ``ports`` is an optional :class:`AnalysisPorts` bundle. ``None`` (the
    production default) resolves through :func:`build_analysis_ports`;
    tests pass a scripted :class:`AnalysisPorts` directly to drive the
    pipeline against a deterministic media layout without monkey-patching
    any module-level name in this file.
    """
    if ports is None:
        from src.tasks._container import get_analysis_ports

        ports = get_analysis_ports()

    with task_db_session() as db:
        last_prompt_text: str | None = None
        last_response_text: str | None = None
        last_raw_response_text: str | None = None

        plan = ports.plan_analysis(db, session_id)
        claim, skip = claim_session_for_analysis(
            db,
            session_id=session_id,
            priority=priority,
            queue_task_id=queue_task_id,
            analysis_run_id=plan.analysis_run_id,
        )
        if claim is None and skip is None:
            return finalize_stale_message()
        if claim is None:
            assert skip is not None
            task_log = (
                db.query(TaskLog)
                .filter_by(task_target_id=session_id, task_type="session_analysis")
                .order_by(TaskLog.id.desc())
                .first()
            )
            assert task_log is not None
            return finalize_skip(
                db,
                task_log=task_log,
                session_id=session_id,
                outcome=skip,
                priority=priority,
            )
        try:
            home_context = build_home_context(db)
            client, provider = _build_provider_client(db)
        except Exception as exc:
            # The claim (line above) already flipped the TaskLog to RUNNING;
            # a failure here (e.g. provider API-key decryption) must still
            # finalize it to FAILED or the row is stranded as a zombie
            # "running" (no finished_at/message) and the session stays
            # analyzing forever.
            _handle_top_level_failure(
                db,
                self=self,
                claim=claim,
                exc=exc,
                last_prompt_text=None,
                last_response_text=None,
                last_raw_response_text=None,
                priority=priority,
            )
            raise
        success_return: dict[str, Any]

        try:
            loop_result = _run_sub_chunk_loop(
                db=db,
                self=self,
                claim=claim,
                plan=plan,
                home_context=home_context,
                client=client,
                provider=provider,
                priority=priority,
                ports=ports,
            )
            if isinstance(loop_result, dict):
                return loop_result
            sub_chunk_count, parse_modes, late_worker = loop_result
            if late_worker:
                return {"late_worker": True, "session_id": session_id}
            try:
                success_return = finalize_session_success(
                    db,
                    claim=claim.to_guards(),
                    session=claim.session,
                    task_log=claim.task_log,
                    analysis_run_id=plan.analysis_run_id,
                    file_count=len(plan.sub_chunks),
                    sub_chunk_count=sub_chunk_count,
                    priority=priority,
                    raw_mp4_num_frames=RAW_MP4_NUM_FRAMES,
                    parse_modes=parse_modes,
                    replace_session_events_fn=ports.replace_session_events,
                )
            except Exception as exc:
                raw_text = getattr(client, "get_last_raw_response_text", lambda: None)()
                if raw_text:
                    last_raw_response_text = raw_text
                last_response_text = last_raw_response_text or last_response_text
                _handle_top_level_failure(
                    db,
                    self=self,
                    claim=claim,
                    exc=exc,
                    last_prompt_text=last_prompt_text,
                    last_response_text=last_response_text,
                    last_raw_response_text=last_raw_response_text,
                    priority=priority,
                )
                raise
        finally:
            close = getattr(client, "close", None)
            if close is not None:
                close()
        return success_return


def _run_sub_chunk_loop(
    *,
    db: Any,
    self: Any,
    claim: Any,
    plan: Any,
    home_context: dict[str, Any],
    client: Any,
    provider: Any,
    priority: str,
    ports: AnalysisPorts,
) -> tuple[int, list[str], bool] | dict[str, Any]:
    """Iterate over the file-level plan; one short txn per sub-chunk stage.

    Returns ``(sub_chunk_count, parse_modes, late_worker)`` on the
    normal / late-worker paths, or a structured ``dict`` (the same
    shape :func:`cancel_session` emits) when the loop exits through
    the ``TaskCancellationRequested`` branch. The orchestrator
    dispatches on ``isinstance(..., dict)`` before unpacking the
    tuple.

    The orchestration session ``db`` stays open for the lifetime of
    the call so the in-memory ``claim.session`` / ``claim.task_log``
    stay attached; per-sub-chunk DB writes go through the dedicated
    :func:`_run_one_sub_chunk` helper which opens a fresh
    transaction (released between the pre / post stages so the LLM
    HTTP call happens with **no** checked-out connection).

    The media-layout helpers reach this loop through the injected
    ``ports`` bundle; tests inject a scripted :class:`AnalysisPorts`
    rather than monkey-patching the underlying helper symbols.
    """
    guards = claim.to_guards()
    sub_chunk_count = 0
    parse_modes: list[str] = []
    last_prompt_text: str | None = None
    last_response_text: str | None = None
    last_raw_response_text: str | None = None
    try:
        # Mirror the pre-Wave-5 cancel-fence placement: checked once
        # after the claim succeeds (before any chunk work) so a cancel
        # request that arrived during the claim is honoured even when
        # only one sub-chunk would otherwise have been processed.
        ensure_task_not_cancelled(
            db,
            claim.task_log.id,
            default_message=f"Analysis cancelled for session {claim.session.id}",
        )
        for plan_item in plan.sub_chunks:
            ensure_task_not_cancelled(
                db,
                claim.task_log.id,
                default_message=(
                    f"Analysis cancelled for session {claim.session.id}, "
                    f"file {plan_item.sub_chunk_index}"
                ),
            )
            system_prompt, user_prompt = _build_prompts(
                claim.source,
                home_context,
                claim.session,
                plan_item,
            )
            last_prompt_text = user_prompt
            video_data_url = ports.video_data_url(plan_item.file_paths[0])
            extra_body = build_sub_chunk_extra_body()
            prepared, result = _run_one_sub_chunk(
                db=db,
                self=self,
                claim=claim,
                guards=guards,
                plan_item=plan_item,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                video_data_url=video_data_url,
                extra_body=extra_body,
                client=client,
                provider=provider,
            )
            if result is not None:
                last_response_text = getattr(result, "raw_response_text", None) or ""
                last_raw_response_text = last_response_text
            if prepared.did_call_llm:
                sub_chunk_count += 1
                parse_modes.append("new")
        # Post-loop cancel fence: a cancel that arrives between the
        # last sub-chunk's commit and the completion query must still
        # roll the session back to ``SEALED`` rather than to ``PARTIAL``.
        ensure_task_not_cancelled(
            db,
            claim.task_log.id,
            default_message=f"Analysis cancelled for session {claim.session.id}",
        )
    except TaskCancellationRequested as exc:
        return cancel_session(
            db,
            task_log_id=claim.task_log.id,
            session_id=claim.session.id,
            priority=priority,
            last_chunk_index=None,
            last_sub_chunk_index=None,
            message=str(exc),
        )
    except DeadlockRetrySignal as exc:
        handle_deadlock_retry_exhausted(db, exc=exc, claim=claim, session_id=claim.session.id)
        raise self.retry(exc=exc.original, countdown=2 ** (self.request.retries + 1)) from exc
    except LateWorkerFencingError as exc:
        logger.info(
            "Late worker detected for session %s: %s; exiting cleanly",
            claim.session.id,
            exc,
        )
        return sub_chunk_count, parse_modes, True
    except Exception as exc:  # noqa: BLE001 — boundary handler for the orchestrator
        return _handle_loop_exception(
            db=db,
            self=self,
            claim=claim,
            guards=guards,
            priority=priority,
            last_prompt_text=last_prompt_text,
            last_response_text=last_response_text,
            last_raw_response_text=last_raw_response_text,
            exc=exc,
            sub_chunk_count=sub_chunk_count,
            parse_modes=parse_modes,
        )
    return sub_chunk_count, parse_modes, False


def _handle_loop_exception(
    *,
    db: Any,
    self: Any,
    claim: Any,
    guards: ClaimGuards,
    priority: str,
    last_prompt_text: str | None,
    last_response_text: str | None,
    last_raw_response_text: str | None,
    exc: BaseException,
    sub_chunk_count: int,
    parse_modes: list[str],
) -> tuple[int, list[str], bool]:
    """Unwrap + classify an exception escaped from the sub-chunk loop.

    Mirrors the pre-Wave-5 orchestrator's exception handler: a PG
    deadlock triggers ``self.retry``; a
    :class:`LateWorkerFencingError` becomes a silent no-op; any
    other exception marks the session PARTIAL (if a sub-chunk was
    in-flight) or FAILED (if no work had started) and re-raises so
    Celery records the failure.
    """
    if isinstance(exc, RawResponseCapture):
        last_raw_response_text = exc.raw_response_text or last_raw_response_text
        last_response_text = last_raw_response_text or last_response_text
        exc = exc.original
    if is_deadlock_operational_error(exc) and self.request.retries < DEADLOCK_MAX_RETRIES:
        from src.services.analysis.claim import mark_session_sealed_for_retry

        mark_session_sealed_for_retry(db, claim.session.id)
        claim.task_log.retry_count = self.request.retries + 1
        claim.task_log.message = (
            f"Deadlock detected, retry {self.request.retries + 1}/"
            f"{DEADLOCK_MAX_RETRIES} in {2**self.request.retries}s"
        )
        db.commit()
        raise self.retry(exc=exc, countdown=2**self.request.retries) from exc
    current_checkpoint = (
        db.query(SessionAnalysisCheckpoint)
        .filter(
            SessionAnalysisCheckpoint.session_id == claim.session.id,
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
        last_prompt_text=last_prompt_text,
        last_response_text=last_response_text,
        last_raw_response_text=last_raw_response_text,
        priority=priority,
        session_id=claim.session.id,
    )
    if current_checkpoint is not None:
        record_partial_failure_transition(db, session=claim.session, task_log=claim.task_log)
    else:
        record_failed_transition(db, session=claim.session, task_log=claim.task_log)
    db.commit()
    logger.exception(
        "Failed to analyze session %s, prompt=%r, raw_response=%r",
        claim.session.id,
        truncate_text(last_prompt_text, 4000),
        truncate_text(last_raw_response_text, 16000) or truncate_text(last_response_text, 16000),
    )
    raise exc from None


class _PreparedSubChunk:
    """Per-sub-chunk state produced by the ``pre`` stage."""

    __slots__ = (
        "checkpoint_id",
        "did_call_llm",
        "extra_body",
        "plan_item",
        "system_prompt",
        "user_prompt",
        "video_data_url",
    )

    def __init__(
        self,
        *,
        checkpoint_id: int,
        system_prompt: str,
        user_prompt: str,
        plan_item: Any,
        video_data_url: str,
        extra_body: dict[str, Any],
        did_call_llm: bool,
    ) -> None:
        self.checkpoint_id = checkpoint_id
        self.system_prompt = system_prompt
        self.user_prompt = user_prompt
        self.plan_item = plan_item
        self.video_data_url = video_data_url
        self.extra_body = extra_body
        self.did_call_llm = did_call_llm


def _run_one_sub_chunk(
    *,
    db: Any,
    self: Any,
    claim: Any,
    guards: ClaimGuards,
    plan_item: SubChunkPlan,
    system_prompt: str,
    user_prompt: str,
    video_data_url: str,
    extra_body: dict[str, Any],
    client: Any,
    provider: Any,
) -> tuple[_PreparedSubChunk, Any]:
    """Open / close a fresh transaction around the LLM HTTP call.

    ``pre_db`` writes the ``processing`` state + lease renewal;
    ``post_db`` writes the ``success`` state + LLMUsageLog + lease
    renewal. Both transactions use a NEW ``task_db_session`` so the
    LLM call sits between two connection-released windows — the
    structural enforcement of "no DB connection during LLM call".

    When the checkpoint is already ``state=success`` from a
    previous partial run, the function returns without calling the
    vision provider — the resumed run only re-charges the failed
    sub-chunks.
    """
    with task_db_session() as pre_db:
        checkpoint = get_or_create_checkpoint(
            pre_db,
            session_id=claim.session.id,
            claim=guards,
            sub_chunk=plan_item,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            video_data_url=video_data_url,
        )
        did_call_llm = checkpoint.state != "success"
        if did_call_llm:
            mark_checkpoint_processing(
                pre_db,
                claim=guards,
                checkpoint=checkpoint,
                lease_owner=claim.task_log.lease_owner or "",
            )
            renew_task_lease(pre_db, claim.task_log.id, claim.task_log.lease_owner or "")
            enforce_token_quota(pre_db, provider)
        pre_db.commit()
        checkpoint_id = checkpoint.id
        prepared = _PreparedSubChunk(
            checkpoint_id=checkpoint_id,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            plan_item=plan_item,
            video_data_url=video_data_url,
            extra_body=extra_body,
            did_call_llm=did_call_llm,
        )

    if not did_call_llm:
        # Resumed run; the LLM call is intentionally skipped.
        return prepared, None

    # LLM call — no DB connection held here.
    try:
        result = execute_sub_chunk(
            client=client,
            provider=provider,
            sub_chunk=plan_item,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            video_data_url=video_data_url,
            extra_body=extra_body,
            response_parser=parse_video_recognition_output,
        )
    except Exception as exc:
        # If the LLM call or the parser raised, capture the raw
        # gateway response (already captured inside
        # ``execute_sub_chunk`` before parsing) so the failure log
        # carries the same payload the pre-Wave-5 monolith used to
        # surface. Re-raise the original exception so the
        # orchestrator's deadlock / permanent-failure branches see
        # the real cause.
        raw_text = getattr(client, "get_last_raw_response_text", lambda: None)()
        if isinstance(exc, RawResponseCapture):
            raise
        raise RawResponseCapture(exc, raw_text) from exc

    with task_db_session() as post_db:
        post_db_checkpoint = (
            post_db.query(SessionAnalysisCheckpoint)
            .filter(SessionAnalysisCheckpoint.id == checkpoint_id)
            .one()
        )
        write_sub_chunk_checkpoint(
            post_db,
            claim=guards,
            checkpoint=post_db_checkpoint,
            recognition_result=result.recognition_result,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            total_tokens=result.total_tokens,
        )
        write_token_usage(
            post_db,
            provider=provider,
            checkpoint=post_db_checkpoint,
            session_id=claim.session.id,
            usage=result.usage,
            record_token_usage_fn=record_token_usage,
        )
        renew_task_lease(post_db, claim.task_log.id, claim.task_log.lease_owner or "")
        post_db.commit()
    return prepared, result


def _handle_top_level_failure(
    db: Any,
    *,
    self: Any,
    claim: Any,
    exc: BaseException,
    last_prompt_text: str | None,
    last_response_text: str | None,
    last_raw_response_text: str | None,
    priority: str,
) -> None:
    """Top-level error handler: deadlock retry, FAILED finalize, raise."""
    if is_deadlock_operational_error(exc) and self.request.retries < DEADLOCK_MAX_RETRIES:
        from src.services.analysis.claim import mark_session_sealed_for_retry

        mark_session_sealed_for_retry(db, claim.session.id)
        claim.task_log.retry_count = self.request.retries + 1
        claim.task_log.message = (
            f"Deadlock detected, retry {self.request.retries + 1}/"
            f"{DEADLOCK_MAX_RETRIES} in {2**self.request.retries}s"
        )
        db.commit()
        raise self.retry(exc=exc, countdown=2**self.request.retries) from exc

    current_checkpoint = (
        db.query(SessionAnalysisCheckpoint)
        .filter(
            SessionAnalysisCheckpoint.session_id == claim.session.id,
            SessionAnalysisCheckpoint.analysis_run_id == claim.to_guards().analysis_run_id,
            SessionAnalysisCheckpoint.state == "processing",
        )
        .order_by(SessionAnalysisCheckpoint.id.desc())
        .first()
    )
    if current_checkpoint is not None:
        mark_checkpoint_error(db, claim=claim.to_guards(), checkpoint=current_checkpoint, error=exc)
    record_recovery_failure(
        db,
        claim=claim.to_guards(),
        task_log=claim.task_log,
        error=exc,
        failed_chunk_index=None,
        failed_sub_chunk_index=None,
        last_prompt_text=last_prompt_text,
        last_response_text=last_response_text,
        last_raw_response_text=last_raw_response_text,
        priority=priority,
        session_id=claim.session.id,
    )
    if current_checkpoint is not None:
        record_partial_failure_transition(db, session=claim.session, task_log=claim.task_log)
    else:
        record_failed_transition(db, session=claim.session, task_log=claim.task_log)
    db.commit()
    logger.exception(
        "Failed to analyze session %s, prompt=%r, raw_response=%r",
        claim.session.id,
        truncate_text(last_prompt_text, 4000),
        truncate_text(last_raw_response_text, 16000) or truncate_text(last_response_text, 16000),
    )
