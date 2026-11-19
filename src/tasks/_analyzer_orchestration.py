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
* reached through the :class:`~src.services.analysis.contracts.AnalysisPorts`
  bundle — the plan / video-URL / event-replace / frame-set helpers.
  Tests inject a scripted :class:`AnalysisPorts` via the task-layer holder
  (:func:`src.tasks._container.set_analysis_ports_for_tests`) or via
  the ``ports`` kwarg on :func:`run_session_analysis`, so the
  orchestrator no longer needs monkey-patchable names for those
  helpers.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any

from src.application.prompt.compiler import compile_video_recognition_prompt
from src.application.prompt.contracts import (
    SessionPromptContext,
    StrategyPromptContext,
    VideoRecognitionPromptInput,
    VideoSourcePromptContext,
)
from src.core.pipeline_constants import SessionAnalysisStatus, SourceType, TaskStatus
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
    claim_checkpoint_before_sampling,
    claim_session_for_analysis,
    finalize_session_success,
    finalize_skip,
    finalize_stale_message,
    frames_mode_frame_fingerprint,
    get_or_create_checkpoint,
    is_deadlock_operational_error,
    mark_checkpoint_error,
    mark_checkpoint_processing,
    record_recovery_failure,
    write_sub_chunk_checkpoint,
    write_token_usage,
)
from src.services.analysis.claim import handle_deadlock_retry_exhausted
from src.services.analysis.contracts import DEADLOCK_MAX_RETRIES, AnalysisPorts, RawResponseCapture
from src.services.analysis.execute import build_provider_client, enforce_fencing, execute_sub_chunk
from src.services.analysis.finalize import (
    completed_results_for_run,
    merge_session_fields,
    record_failed_transition,
    record_partial_failure_transition,
    replace_session_events,
)
from src.services.analysis.plan import SubChunkPlan
from src.services.attention import attention_keys_for_db
from src.services.dispatch import ensure_task_not_cancelled, finalize_task_log
from src.services.dispatch.cancel import TaskCancellationRequested
from src.services.dispatch.lease import renew_task_lease
from src.services.frame_sampling import (
    INPUT_MODE_IMAGE,
    INPUT_MODE_VIDEO,
    FramePlan,
    compute_frame_plan,
    conservative_tokens_per_image,
    content_signature,
    estimate_prompt_tokens,
    is_frame_acquisition_failure,
    record_frame_sampling_failure,
    record_frame_sampling_success,
    resolve_input_mode,
)
from src.services.frame_sampling.policy import FramePolicy, parse_frame_policy
from src.services.frame_sampling.types import FrameSample
from src.services.home_profile import build_home_context
from src.services.llm_output_utils import truncate_text
from src.services.llm_qos import enforce_token_quota, record_token_usage
from src.services.pipeline_state import transition_session, transition_task_log
from src.services.prompt_builder.v2.video_recognition import build_strategy_note
from src.services.provider_generation_limits import (
    get_generation_limits,
    resolve_image_output_tokens,
    resolve_num_frames,
)
from src.services.system_config_registry import FRAME_SAMPLING_SESSION_FRAME_LIMIT, get_config
from src.services.video_analysis.enums import VIDEO_EVENT_TYPES
from src.services.video_analysis.mapper import validate_recognition_offsets_within_sub_chunk
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
    *,
    input_mode: str = INPUT_MODE_VIDEO,
    frame_timestamps: Sequence[float] = (),
) -> tuple[str, str]:
    """Assemble the recognition prompts for one sub-chunk work item.

    ``input_mode`` / ``frame_timestamps`` default to the video-mode values so
    the raw_mp4 path renders byte-identically; image mode passes ``"image"``
    plus the timestamps of the frames actually sent to the model.

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
            input_mode=input_mode,
            frame_timestamps=list(frame_timestamps),
        )
    )


def _resolve_image_budget(provider: Any) -> tuple[int, FramePolicy, int]:
    """Resolve ``(max_model_len, policy, max_output_tokens)`` once per file."""
    max_model_len = get_generation_limits(provider).max_model_len
    if max_model_len is None:
        raise ValueError("image input mode requires max_model_len on the provider")
    return max_model_len, parse_frame_policy(provider), resolve_image_output_tokens(provider)


def _derive_frame_plan(
    *,
    plan_item: SubChunkPlan,
    policy: FramePolicy,
    max_model_len: int,
    max_output_tokens: int,
    prompt_tokens_est: int,
) -> tuple[FramePlan, dict[str, Any]]:
    """Derive the frame plan + the ``TaskLog.detail_json["frame_plan"]`` payload.

    The per-image token cost is the 1440p 16:9 box upper bound
    (:func:`conservative_tokens_per_image` over ``policy.max_edge``) — no
    source-resolution probe: the sampler does not scale sources inside the
    ``max_edge`` box, so no frame can cost more than the box, and cheaper
    frames only leave budget headroom. The same 16:9 box (``max_edge`` x
    ``max_edge * 9 // 16``) is fed to :func:`compute_frame_plan` as the frame
    dimensions, so the recorded ``tokens_per_image`` equals
    ``plan.tokens_per_image`` exactly.
    """
    plan = compute_frame_plan(
        context_size=max_model_len,
        policy=policy,
        duration_seconds=plan_item.duration_seconds,
        prompt_tokens_est=prompt_tokens_est,
        frame_width=policy.max_edge,
        frame_height=policy.max_edge * 9 // 16,
        max_output_tokens=max_output_tokens,
        duration_source=plan_item.duration_source,
    )
    frame_plan_detail = {
        "context_size": max_model_len,
        "max_model_len": max_model_len,
        "tokens_per_image": conservative_tokens_per_image(max_edge=policy.max_edge),
        "tokens_assumption": "1440p_box_16x9",
        "prompt_tokens_est": prompt_tokens_est,
        "max_output_tokens": max_output_tokens,
        "frames": plan.frames,
        "clamp_reason": plan.clamp_reason,
        "duration_seconds": plan_item.duration_seconds,
        "duration_source": plan_item.duration_source,
        "sampler_ms": None,
    }
    return plan, frame_plan_detail


def _synthetic_frame_timestamps(duration_seconds: float, frame_count: int) -> tuple[float, ...]:
    """The timestamps :func:`compute_frame_plan` emits for ``frame_count`` frames."""
    return tuple(index * duration_seconds / frame_count for index in range(frame_count))


def _build_image_prompts(
    source: VideoSource,
    home_context: dict[str, Any],
    session: Any,
    sub_chunk: SubChunkPlan,
    frame_timestamps: Sequence[float],
) -> tuple[str, str]:
    """Render the recognition prompts in the ``input_mode="image"`` branch."""
    return _build_prompts(
        source,
        home_context,
        session,
        sub_chunk,
        input_mode=INPUT_MODE_IMAGE,
        frame_timestamps=frame_timestamps,
    )


def _plan_image_prompts(
    *,
    provider: Any,
    plan_item: SubChunkPlan,
    source: VideoSource,
    home_context: dict[str, Any],
    session: Any,
) -> tuple[FramePolicy, FramePlan, dict[str, Any], str, str]:
    """Converge the frame plan with the image prompt's frame-list block.

    ``compute_frame_plan`` needs ``prompt_tokens_est`` before the frames are
    known, while the image prompt's size depends on the frame count (one
    ``帧 i/N — t=X.Xs`` line per frame). The dependency is monotone: a longer
    frame list only increases the estimate, and a larger estimate can only
    keep or reduce the planned count. Starting from the smallest legal list
    (one frame) gives an upper bound ``k0``; re-rendering with ``k0`` lines
    yields ``k1 <= k0`` and one final round yields ``k2 ∈ [k1, k0]``.

    The plan handed to the sampler is the one whose estimate was rendered
    with at least as many lines as it plans to acquire: when ``k2 <= k1``
    the second re-plan wins (estimate rendered with ``k1`` lines); when
    ``k2 > k1`` the first re-plan wins (estimate rendered with ``k0``
    lines) because re-planning with fewer lines would understate the prompt
    the next round would render. Either way the final rendered prompt cannot
    outgrow the budget by more than rounding; the caller re-checks the real
    prompt against ``max_model_len`` after acquisition and fails loudly on
    overflow.
    """
    max_model_len, policy, max_output_tokens = _resolve_image_budget(provider)

    def _render(frame_count: int) -> tuple[str, str]:
        return _build_image_prompts(
            source,
            home_context,
            session,
            plan_item,
            _synthetic_frame_timestamps(plan_item.duration_seconds, frame_count),
        )

    def _plan(frame_count: int) -> tuple[FramePlan, dict[str, Any]]:
        system_prompt, user_prompt = _render(frame_count)
        return _derive_frame_plan(
            plan_item=plan_item,
            policy=policy,
            max_model_len=max_model_len,
            max_output_tokens=max_output_tokens,
            prompt_tokens_est=estimate_prompt_tokens(system_prompt, user_prompt),
        )

    probe_plan, _ = _plan(1)
    first_plan, first_detail = _plan(probe_plan.frames)
    second_plan, second_detail = _plan(first_plan.frames)
    if second_plan.frames <= first_plan.frames:
        frame_plan, frame_plan_detail = second_plan, second_detail
    else:
        frame_plan, frame_plan_detail = first_plan, first_detail
    system_prompt, user_prompt = _render(frame_plan.frames)
    logger.debug(
        "Session %s file %s frame-plan convergence: probe=%d first=%d second=%d "
        "chosen=%d clamp=%s prompt_tokens_est=%d",
        session.id,
        plan_item.sub_chunk_index,
        probe_plan.frames,
        first_plan.frames,
        second_plan.frames,
        frame_plan.frames,
        frame_plan.clamp_reason,
        frame_plan_detail["prompt_tokens_est"],
    )
    return policy, frame_plan, frame_plan_detail, system_prompt, user_prompt


def _check_image_prompt_budget(
    *,
    session_id: int,
    file_path: str,
    system_prompt: str,
    user_prompt: str,
    frame_count: int,
    frame_plan: FramePlan,
    frame_plan_detail: dict[str, Any],
) -> None:
    """Fail loudly when the acquired frame set outgrows ``max_model_len``.

    The pre-sampler plan is conservative (its estimate covers at least as
    many frame-list lines as it plans to acquire), but the sampler returns
    the real frames and the prompt is rebuilt from their timestamps. This
    guard is the last line of defence: silently sending a request beyond the
    model context would be truncated or rejected by the provider.
    """
    prompt_tokens_est = estimate_prompt_tokens(system_prompt, user_prompt)
    budget_used = (
        prompt_tokens_est
        + frame_plan_detail["max_output_tokens"]
        + frame_count * frame_plan.tokens_per_image
    )
    max_model_len = frame_plan_detail["max_model_len"]
    if budget_used > max_model_len:
        raise ValueError(
            "image-mode request exceeds max_model_len after frame acquisition: "
            f"session={session_id} file={file_path} "
            f"prompt_tokens_est={prompt_tokens_est} "
            f"+ max_output_tokens={frame_plan_detail['max_output_tokens']} "
            f"+ frames={frame_count} * tokens_per_image={frame_plan.tokens_per_image} "
            f"= {budget_used} > max_model_len={max_model_len}"
        )


def _write_frame_plan_detail(db: Any, task_log_id: int, *, frame_plan: dict[str, Any]) -> None:
    """Merge ``frame_plan`` into ``TaskLog.detail_json`` (other keys untouched)."""
    row = db.query(TaskLog).filter(TaskLog.id == task_log_id).one_or_none()
    if row is None:
        raise LateWorkerFencingError(f"TaskLog {task_log_id} disappeared during frame-plan write")
    detail = dict(row.detail_json) if isinstance(row.detail_json, dict) else {}
    detail["frame_plan"] = frame_plan
    row.detail_json = detail


def _fill_frame_plan_sampler_detail(
    db: Any, task_log_id: int, *, sampler_ms: int, frame_source: str
) -> None:
    """Fill ``frame_plan.sampler_ms`` / ``frame_source`` once the sampler returns."""
    row = db.query(TaskLog).filter(TaskLog.id == task_log_id).one_or_none()
    if row is None:
        raise LateWorkerFencingError(f"TaskLog {task_log_id} disappeared during frame-plan write")
    detail = dict(row.detail_json) if isinstance(row.detail_json, dict) else {}
    existing = detail.get("frame_plan")
    frame_plan = dict(existing) if isinstance(existing, dict) else {}
    frame_plan["sampler_ms"] = sampler_ms
    frame_plan["frame_source"] = frame_source
    detail["frame_plan"] = frame_plan
    row.detail_json = detail


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
    from src.application.container_holder import get_container
    from src.tasks._container import get_analysis_ports

    if ports is None:
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
            client, provider = build_provider_client(db, llm_factory=get_container().llm_factory)
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
            generation_limits = get_generation_limits(provider)
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
                    raw_mp4_num_frames=resolve_num_frames(
                        provider, scene_default=RAW_MP4_NUM_FRAMES
                    ),
                    media_input_budget=generation_limits.input_tokens(scene_default=8192),
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


def _claim_owns_task_log(guards: ClaimGuards) -> bool:
    """Return True when ``guards`` still match the live TaskLog row.

    Reads the row through a fresh session: the long-lived orchestration
    session may hold a stale in-memory TaskLog while the heartbeat bumps the
    generation from another connection. ``False`` means a recovery worker
    owns the task and every row must be left to that owner.
    """
    try:
        with task_db_session() as check_db:
            enforce_fencing(check_db, claim=guards)
    except LateWorkerFencingError:
        return False
    return True


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
    input_mode = resolve_input_mode(provider)
    # Session frame budget: only image mode acquires frames, so the limit
    # (system config ``frame_sampling_session_frame_limit``, 0 = off) is
    # read exactly once per run here and never queried again inside the
    # loop. With 0 (default / absent) the pre-check below never fires and
    # the run behaves exactly as before.
    frame_budget_limit = (
        int(get_config(db, FRAME_SAMPLING_SESSION_FRAME_LIMIT))
        if input_mode == INPUT_MODE_IMAGE
        else 0
    )
    frames_acquired = 0
    files_analyzed = 0
    try:
        # Mirror the cancel-fence placement: checked once
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
            if input_mode == INPUT_MODE_IMAGE:
                file_path = plan_item.file_paths[0]
                (
                    policy,
                    frame_plan,
                    frame_plan_detail,
                    system_prompt,
                    user_prompt,
                ) = _plan_image_prompts(
                    provider=provider,
                    plan_item=plan_item,
                    source=claim.source,
                    home_context=home_context,
                    session=claim.session,
                )
                last_prompt_text = user_prompt
                planned_total = frames_acquired + frame_plan.frames
                if frame_budget_limit > 0 and planned_total > frame_budget_limit:
                    # Budget pre-check: acquiring this file's planned frames
                    # would exceed the session limit, so stop before sampling
                    # or calling the LLM. This is a deliberate stop, not an
                    # error: the session ends PARTIAL (never FAILED) with the
                    # already-analysed files' events kept, and the run
                    # finalizes as SUCCESS with a frame_budget_exhausted
                    # detail. A single file planning more than the whole
                    # budget lands here on the first iteration (provider
                    # validation already rejects such configs at save time;
                    # this is the analysis-time backstop).
                    logger.info(
                        "Session %s frame budget exhausted: limit=%d frames_acquired=%d "
                        "files_analyzed=%d files_skipped=%d; skipping remaining file(s)",
                        claim.session.id,
                        frame_budget_limit,
                        frames_acquired,
                        files_analyzed,
                        len(plan.sub_chunks) - files_analyzed,
                    )
                    return _finalize_session_frame_budget_exhausted(
                        db=db,
                        claim=claim,
                        guards=guards,
                        plan=plan,
                        file_count=len(plan.sub_chunks),
                        frame_budget_limit=frame_budget_limit,
                        frames_acquired=frames_acquired,
                        files_analyzed=files_analyzed,
                        files_skipped=len(plan.sub_chunks) - files_analyzed,
                        priority=priority,
                        replace_session_events_fn=ports.replace_session_events,
                    )
                # Pre-sampler transaction: persist the frame plan (so a
                # sampler failure still leaves it in detail_json) and own
                # the checkpoint row so the failure is fenced to a real
                # row (checkpoint ``error`` + session ``PARTIAL``).
                with task_db_session() as pre_sample_db:
                    _write_frame_plan_detail(
                        pre_sample_db,
                        claim.task_log.id,
                        frame_plan=frame_plan_detail,
                    )
                    claim_checkpoint_before_sampling(
                        pre_sample_db,
                        session_id=claim.session.id,
                        claim=guards,
                        sub_chunk=plan_item,
                    )
                    pre_sample_db.commit()
                # The transaction above committed a new ``detail_json``
                # outside this session; refresh the in-memory claim so the
                # final finalize merge starts from current state instead of
                # clobbering ``frame_plan``.
                db.refresh(claim.task_log, attribute_names=["detail_json"])
                # Sampler call — no DB session held here (same invariant
                # as the LLM call below).
                frame_set = ports.frame_set(provider, file_path, frame_plan)
                # The budget charges the frames ACTUALLY acquired (the
                # sampler may return fewer than planned); the pre-check
                # above used the plan to avoid an over-budget acquisition.
                frames_acquired += len(frame_set.frames)
                # Rebuild with the REAL timestamps of the frames being sent:
                # the plan-time render above only exists to size the budget.
                system_prompt, user_prompt = _build_image_prompts(
                    claim.source,
                    home_context,
                    claim.session,
                    plan_item,
                    tuple(frame.t_seconds for frame in frame_set.frames),
                )
                last_prompt_text = user_prompt
                _check_image_prompt_budget(
                    session_id=claim.session.id,
                    file_path=file_path,
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    frame_count=len(frame_set.frames),
                    frame_plan=frame_plan,
                    frame_plan_detail=frame_plan_detail,
                )
                frame_fingerprint = frames_mode_frame_fingerprint(
                    frame_set.frames,
                    policy,
                    content_signature=content_signature(file_path),
                )
                prepared, result = _run_one_sub_chunk(
                    db=db,
                    self=self,
                    claim=claim,
                    guards=guards,
                    plan_item=plan_item,
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    video_data_url=None,
                    extra_body={},
                    client=client,
                    provider=provider,
                    frames=frame_set.frames,
                    frame_fingerprint=frame_fingerprint,
                    sampler_elapsed_ms=frame_set.elapsed_ms,
                    frame_source=frame_set.source,
                )
                files_analyzed += 1
            else:
                video_data_url = ports.video_data_url(plan_item.file_paths[0])
                extra_body = build_sub_chunk_extra_body(
                    resolve_num_frames(provider, scene_default=RAW_MP4_NUM_FRAMES)
                )
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
        return _handle_late_worker_fencing(
            db=db,
            self=self,
            claim=claim,
            guards=guards,
            provider=provider,
            priority=priority,
            last_prompt_text=last_prompt_text,
            last_response_text=last_response_text,
            last_raw_response_text=last_raw_response_text,
            exc=exc,
            sub_chunk_count=sub_chunk_count,
            parse_modes=parse_modes,
        )
    except Exception as exc:
        return _handle_loop_exception(
            db=db,
            self=self,
            claim=claim,
            guards=guards,
            provider=provider,
            priority=priority,
            last_prompt_text=last_prompt_text,
            last_response_text=last_response_text,
            last_raw_response_text=last_raw_response_text,
            exc=exc,
            sub_chunk_count=sub_chunk_count,
            parse_modes=parse_modes,
        )
    return sub_chunk_count, parse_modes, False


def _finalize_session_frame_budget_exhausted(
    *,
    db: Any,
    claim: Any,
    guards: ClaimGuards,
    plan: Any,
    file_count: int,
    frame_budget_limit: int,
    frames_acquired: int,
    files_analyzed: int,
    files_skipped: int,
    priority: str,
    replace_session_events_fn: Any = None,
) -> dict[str, Any]:
    """Finalize a run stopped by the session frame budget as PARTIAL.

    The budget stop is deliberate, not a failure: already-committed
    checkpoint results are materialized (events + session projection)
    exactly like the success path, the session transitions
    ANALYZING -> PARTIAL (even when zero files were analysed), and the
    TaskLog finalizes as SUCCESS. ``frame_budget_exhausted`` is merged
    into ``TaskLog.detail_json`` without clobbering existing keys —
    the analysed files' ``frame_plan`` survives.
    """
    if replace_session_events_fn is None:
        replace_session_events_fn = replace_session_events
    enforce_fencing(db, claim=guards)
    # Expire cached session fields so the CAS UPDATE in
    # ``transition_session`` starts from current state (the
    # ``synchronize_session=False`` flag leaves the in-memory copy
    # stale otherwise) — same pattern as the success finalizer.
    db.expire(claim.session)
    structured_results, events_to_persist = completed_results_for_run(
        db, claim.session, plan.analysis_run_id
    )
    merge_session_fields(
        claim.session,
        structured_results,
        events_to_persist,
        attention_keys=attention_keys_for_db(db),
    )
    replaced_deleted_count = replace_session_events_fn(db, claim.session.id, events_to_persist)

    budget_detail = {
        "limit": frame_budget_limit,
        "frames_acquired": frames_acquired,
        "files_analyzed": files_analyzed,
        "files_skipped": files_skipped,
    }
    raw_detail = claim.task_log.detail_json
    detail = dict(raw_detail) if isinstance(raw_detail, dict) else {}
    detail["frame_budget_exhausted"] = budget_detail
    claim.task_log.detail_json = detail

    completed = transition_session(
        db,
        claim.session.id,
        SessionAnalysisStatus.ANALYZING,
        SessionAnalysisStatus.PARTIAL,
        reason="frame_budget_exhausted",
        source="analyze_session_task",
        task_log=claim.task_log,
    )
    if not completed.applied:
        # Lost race (e.g. concurrent cancel took the session): commit nothing,
        # mirror the success-path skip response.
        return {
            "events_created": 0,
            "skipped": True,
            "reason": "completion_transition_conflict",
            "session_id": claim.session.id,
            "priority": priority,
        }
    claim.session.last_analyzed_at = datetime.now(timezone.utc)
    transition_task_log(
        db,
        claim.task_log.id,
        TaskStatus.RUNNING,
        TaskStatus.SUCCESS,
        reason="frame_budget_exhausted",
        source="analyze_session_task",
    )
    finalize_task_log(
        db,
        claim.task_log,
        TaskStatus.SUCCESS,
        f"Analyzed session across {file_count} files; stopped at the frame budget "
        f"(limit {frame_budget_limit}, {frames_acquired} frames acquired, "
        f"{files_analyzed} files analyzed, {files_skipped} skipped).",
        {
            "session_id": claim.session.id,
            "events_created": len(events_to_persist),
            "events_replaced_deleted": replaced_deleted_count,
            "file_count": file_count,
            "recognized_sub_chunks": files_analyzed,
            "priority": priority,
        },
    )
    db.commit()
    return {
        "events_created": len(events_to_persist),
        "file_count": file_count,
        "recognized_sub_chunks": files_analyzed,
        "frame_budget_exhausted": budget_detail,
        "session_id": claim.session.id,
    }


def _handle_late_worker_fencing(
    *,
    db: Any,
    self: Any,
    claim: Any,
    guards: ClaimGuards,
    provider: Any,
    priority: str,
    last_prompt_text: str | None,
    last_response_text: str | None,
    last_raw_response_text: str | None,
    exc: LateWorkerFencingError,
    sub_chunk_count: int,
    parse_modes: list[str],
) -> tuple[int, list[str], bool]:
    """Classify a checkpoint fencing failure by TaskLog ownership.

    When the guards no longer match the TaskLog a recovery worker owns the
    task, so this worker exits without touching any row. When the guards
    still match, the failure came from a checkpoint-level race while we own
    the lease: route it through the standard failure handler so the TaskLog
    reaches a terminal state instead of staying RUNNING (the F3 zombie).
    """
    if not _claim_owns_task_log(guards):
        logger.info(
            "Late worker detected for session %s: %s; exiting cleanly",
            claim.session.id,
            exc,
        )
        return sub_chunk_count, parse_modes, True
    logger.warning(
        "Checkpoint fencing failed for session %s while the task lease is still ours: %s; "
        "finalizing the run as failed",
        claim.session.id,
        exc,
    )
    return _handle_loop_exception(
        db=db,
        self=self,
        claim=claim,
        guards=guards,
        provider=provider,
        priority=priority,
        last_prompt_text=last_prompt_text,
        last_response_text=last_response_text,
        last_raw_response_text=last_raw_response_text,
        exc=exc,
        sub_chunk_count=sub_chunk_count,
        parse_modes=parse_modes,
    )


def _handle_loop_exception(
    *,
    db: Any,
    self: Any,
    claim: Any,
    guards: ClaimGuards,
    provider: Any,
    priority: str,
    last_prompt_text: str | None,
    last_response_text: str | None,
    last_raw_response_text: str | None,
    exc: BaseException,
    sub_chunk_count: int,
    parse_modes: list[str],
) -> tuple[int, list[str], bool]:
    """Unwrap + classify an exception escaped from the sub-chunk loop.

    Mirrors the previous orchestrator's exception handler: a PG
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
    if is_frame_acquisition_failure(exc):
        # Frame-sampling failure counter: one per logical acquisition
        # attempt (internal sampler HTTP retries not counted). Same
        # transaction as the checkpoint error / TaskLog FAILED writes
        # above.
        record_frame_sampling_failure(db, provider_id=provider.id)
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
    video_data_url: str | None,
    extra_body: dict[str, Any],
    client: Any,
    provider: Any,
    frames: Sequence[FrameSample] | None = None,
    frame_fingerprint: str | None = None,
    sampler_elapsed_ms: int | None = None,
    frame_source: str | None = None,
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

    Image mode: the caller supplies the sampled ``frames`` and the
    namespaced ``frame_fingerprint`` (the sampler already ran outside
    any DB session) plus ``sampler_elapsed_ms`` / ``frame_source``;
    ``video_data_url`` is ``None`` and the checkpoint is keyed on the
    frame fingerprint instead of the video data URL.
    """
    has_frames = frames is not None and len(frames) > 0
    with task_db_session() as pre_db:
        if has_frames:
            _fill_frame_plan_sampler_detail(
                pre_db,
                claim.task_log.id,
                sampler_ms=sampler_elapsed_ms or 0,
                frame_source=frame_source or "sampler",
            )
        checkpoint = get_or_create_checkpoint(
            pre_db,
            session_id=claim.session.id,
            claim=guards,
            sub_chunk=plan_item,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            video_data_url=None if has_frames else video_data_url,
            frame_fingerprint=frame_fingerprint if has_frames else None,
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
        if has_frames:
            # The sampler-detail fill committed outside this session;
            # refresh so later in-session merges keep ``frame_plan``.
            db.refresh(claim.task_log, attribute_names=["detail_json"])
        checkpoint_id = checkpoint.id
        prepared = _PreparedSubChunk(
            checkpoint_id=checkpoint_id,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            plan_item=plan_item,
            video_data_url="" if has_frames else video_data_url,
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
            video_data_url=None if has_frames else video_data_url,
            extra_body=extra_body,
            response_parser=parse_video_recognition_output,
            frames=frames if has_frames else None,
            frame_fingerprint=frame_fingerprint if has_frames else None,
        )
        # Sub-chunk boundary guard (both payload modes): an event offset
        # beyond the sub-chunk duration would map the event outside the
        # material the model saw, silently corrupting absolute session
        # time. Rejection re-uses the standard PARTIAL/FAILED handling.
        validate_recognition_offsets_within_sub_chunk(
            result.recognition_result,
            sub_chunk_duration_seconds=plan_item.duration_seconds,
        )
    except Exception as exc:
        # If the LLM call or the parser raised, capture the raw
        # gateway response (already captured inside
        # ``execute_sub_chunk`` before parsing) so the failure log
        # carries the same payload the monolith used to
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
        if has_frames:
            # Frame-sampling counter: one logical acquisition per
            # successful sub-chunk (the sampler client's internal HTTP
            # retries are not counted — see src.services.frame_sampling.counters).
            # Same transaction as the checkpoint success write above.
            record_frame_sampling_success(
                post_db,
                provider_id=provider.id,
                frame_count=len(frames),
                frame_source=frame_source,
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
