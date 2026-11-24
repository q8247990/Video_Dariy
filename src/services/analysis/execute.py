"""Execution stage: provider client, one vision call, and checkpoint persistence."""

from __future__ import annotations

import base64
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.application.ports.llm_gateway import LLMGatewayFactoryPort, LLMGatewayPort
from src.core.pipeline_constants import TaskStatus
from src.core.provider_key_crypto import decrypt_provider_api_key
from src.models.llm_provider import LLMProvider
from src.models.session_analysis_checkpoint import SessionAnalysisCheckpoint
from src.models.task_log import TaskLog
from src.services.analysis.contracts import RAW_MP4_NUM_FRAMES, LateWorkerFencingError
from src.services.analysis.plan import SubChunkPlan, sub_chunk_fingerprint
from src.services.dispatch import finalize_task_log, renew_task_lease
from src.services.frame_sampling import classify_failure_kind
from src.services.frame_sampling.fingerprint import frame_set_fingerprint
from src.services.frame_sampling.policy import FramePolicy
from src.services.frame_sampling.types import FrameSample, FrameSet
from src.services.llm_output_utils import truncate_text
from src.services.llm_qos import record_token_usage
from src.services.pipeline_state import transition_task_log
from src.services.provider_generation_limits import (
    resolve_image_output_tokens,
    resolve_max_output_tokens,
    resolve_num_frames,
)
from src.services.provider_selector import (
    PROVIDER_TYPE_VISION,
    find_required_enabled_provider,
)
from src.services.session_analysis_video import build_video_data_url
from src.services.video_analysis.schemas import RecognitionResultDTO

# --- checkpoint fingerprint namespaces ---
# The two prefixes are deliberately distinct so a fingerprint computed
# for one payload mode can never equal a fingerprint computed for the
# other: a payload-mode switch (or any change behind the namespaced
# value) always invalidates stored checkpoints.
VIDEO_FINGERPRINT_PREFIX = "video:"
FRAMES_FINGERPRINT_PREFIX = "frames:"

#: Transient checkpoint ``input_fingerprint`` for image-mode sub-chunks
#: between the pre-sampler claim (:func:`claim_checkpoint_before_sampling`)
#: and the sampler returning. The claim must own a concrete row *before*
#: the sampler call so a sampler failure is fenced to a real row
#: (checkpoint ``error`` + session ``PARTIAL``); the post-sample pre-stage
#: overwrites the placeholder with the real
#: :func:`frames_mode_frame_fingerprint` before any LLM call / success
#: write, so a success row never carries it. The ``frames:`` prefix plus
#: the non-hex literal ``"pending"`` can never collide with a real digest
#: or a ``video:`` value.
PROVISIONAL_FRAMES_FINGERPRINT = "frames:pending"


def video_mode_frame_fingerprint(video_data_url: str) -> str:
    """Namespaced checkpoint fingerprint for the raw_mp4 video payload.

    This is today's invalidation value, namespaced:
    ``"video:" + sha256(video_data_url)``. Any change of the video
    payload — or a switch to image mode — changes the value, which
    invalidates any stored checkpoint.
    """
    return f"{VIDEO_FINGERPRINT_PREFIX}{sha256(video_data_url.encode()).hexdigest()}"


def frames_mode_frame_fingerprint(
    frames: Sequence[FrameSample] | FrameSet,
    policy: FramePolicy,
    *,
    content_signature: str,
    extra: dict[str, object] | None = None,
) -> str:
    """Namespaced checkpoint fingerprint for the sampled-frame image payload.

    ``"frames:" + frame_set_fingerprint(...)`` — covers the frame
    positions, the per-frame JPEG digests, the resolved policy and the
    source content signature. A policy change (or any other input
    change) changes the value, which invalidates any stored checkpoint.
    """
    digest = frame_set_fingerprint(
        frames,
        policy,
        content_signature=content_signature,
        extra=extra,
    )
    return f"{FRAMES_FINGERPRINT_PREFIX}{digest}"


# --- provider.py ---
def build_provider_client(
    db: Session,
    *,
    llm_factory: LLMGatewayFactoryPort,
) -> tuple[LLMGatewayPort, LLMProvider]:
    """Resolve the default vision provider and build a fresh gateway.

    The factory is stateless; ``build`` returns a new
    :class:`LLMGatewayPort` (and underlying ``OpenAIClient``) per
    call, which the slim Celery task closes in its error branch.
    """
    provider = find_required_enabled_provider(db, PROVIDER_TYPE_VISION)
    client = llm_factory.build(
        api_base_url=provider.api_base_url,
        api_key=decrypt_provider_api_key(provider.api_key),
        model_name=provider.model_name,
        timeout_seconds=provider.timeout_seconds,
    )
    return client, provider


# --- sub_chunk_runner.py ---
@dataclass(frozen=True)
class SubChunkRunResult:
    """Return value of :func:`execute_sub_chunk`.

    Carries everything the checkpoint writer needs to persist a
    success row: the parsed DTO, the token usage extracted from the
    last gateway call, and the raw response text (used as the
    ``last_raw_response_text`` snapshot in failure logs).

    ``payload_mode`` records which vision payload this call sent:
    ``"image"`` when the caller supplied sampled frames (interleaved
    ``image_url`` parts, no ``media_io_kwargs`` hint) and ``"video"`` for
    the raw_mp4 path (``video_url`` part + the ``num_frames`` hint).

    ``frame_fingerprint`` is the namespaced checkpoint fingerprint this
    run used — the value the caller should pass to
    :func:`get_or_create_checkpoint` so the stored checkpoint matches
    this run's input (``"video:..."`` for the legacy video path,
    ``"frames:..."`` for image mode).

    ``video_data_url`` is the raw_mp4 data URL in video mode; in image
    mode it is ``""`` (no video payload was read or sent).
    """

    recognition_result: RecognitionResultDTO
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    usage: dict[str, int]
    raw_response_text: str | None
    video_data_url: str
    extra_body: dict[str, Any]
    payload_mode: str
    frame_fingerprint: str


# --- sub_chunk_runner.py ---
def build_image_content(frames: Sequence[FrameSample], *, user_prompt: str) -> list[dict[str, Any]]:
    """Build the interleaved image payload for one vision call.

    For ``N`` frames the user content is exactly ``2*N + 1`` parts::

        [text("帧 1/N — t=<s>s"), image, text("帧 2/N — t=<s>s"), image,
         ..., text("帧 N/N — t=<s>s"), image, text(user_prompt)]

    Each image is a ``data:image/jpeg;base64`` URL of the frame's JPEG
    bytes; the label timestamp is the frame position formatted with one
    decimal second. Frame order matches the input order/timestamps and the
    trailing part is the user prompt verbatim.
    """
    content: list[dict[str, Any]] = []
    total = len(frames)
    for index, frame in enumerate(frames, start=1):
        content.append({"type": "text", "text": f"帧 {index}/{total} — t={frame.t_seconds:.1f}s"})
        jpeg_base64 = base64.b64encode(frame.jpeg_bytes).decode("utf-8")
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{jpeg_base64}"},
            }
        )
    content.append({"type": "text", "text": user_prompt})
    return content


# --- sub_chunk_runner.py ---
def _build_messages(
    *,
    system_prompt: str,
    user_prompt: str,
    video_data_url: str,
    frames: Sequence[FrameSample] | None = None,
) -> list[dict[str, Any]]:
    if frames:
        user_content: list[dict[str, Any]] = build_image_content(frames, user_prompt=user_prompt)
    else:
        user_content = [
            {"type": "video_url", "video_url": {"url": video_data_url}},
            {"type": "text", "text": user_prompt},
        ]
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]


# --- sub_chunk_runner.py ---
def _token_counts_from_usage(
    usage: dict[str, int] | None,
) -> tuple[int, int, int]:
    prompt_tokens = int((usage or {}).get("prompt_tokens") or 0)
    completion_tokens = int((usage or {}).get("completion_tokens") or 0)
    total_tokens = int((usage or {}).get("total_tokens") or 0)
    if total_tokens <= 0:
        total_tokens = prompt_tokens + completion_tokens
    return prompt_tokens, completion_tokens, total_tokens


# --- sub_chunk_runner.py ---
def build_sub_chunk_video_url(sub_chunk: SubChunkPlan) -> str:
    """Read the sub-chunk's single MP4 and base64-wrap it.

    Pure function — no LLM, no DB. A sub-chunk always maps to exactly
    one file, so there is no concatenation path.
    """
    if not sub_chunk.file_paths:
        raise ValueError(
            f"sub-chunk {sub_chunk.chunk_index}-{sub_chunk.sub_chunk_index} has no source file"
        )
    return build_video_data_url(sub_chunk.file_paths[0])


# --- sub_chunk_runner.py ---
def build_sub_chunk_extra_body(num_frames: int) -> dict[str, Any]:
    """Return the ``media_io_kwargs.video.num_frames`` hint."""
    return {"media_io_kwargs": {"video": {"num_frames": num_frames}}}


# --- sub_chunk_runner.py ---
def execute_sub_chunk(
    *,
    client: LLMGatewayPort,
    provider: LLMProvider,
    sub_chunk: SubChunkPlan,
    system_prompt: str,
    user_prompt: str,
    video_data_url: str | None = None,
    extra_body: dict[str, Any] | None = None,
    response_parser: Any = None,
    frames: Sequence[FrameSample] | None = None,
    frame_fingerprint: str | None = None,
) -> SubChunkRunResult:
    """Build the vision payload, call the gateway, parse the JSON.

    Holds the DB connection only indirectly: callers must have
    already committed/rolled back before invoking this function. The
    function itself does not import SQLAlchemy and the type checker
    will catch any regression that introduces one.

    The raw LLM response text is captured *before* the response
    parser runs, so a parser exception still surfaces the raw text
    to the failure log — the contract the unit tests
    depend on.

    ``video_data_url`` / ``extra_body`` / ``response_parser`` accept
    pre-computed values from the caller. When ``video_data_url`` is
    None the helper builds it via
    :func:`build_sub_chunk_video_url`; when ``response_parser`` is
    None the helper uses
    :func:`src.services.video_analysis.output_parser.parse_video_recognition_output`.
    The caller is expected to pass values resolved through its own
    monkey-patchable names so the existing unit-test fixtures keep
    working unchanged.

    ``frames`` selects the payload mode: when a non-empty sequence of
    :class:`FrameSample` is supplied the call sends an interleaved image
    payload (``payload_mode == "image"``) with NO ``media_io_kwargs`` hint
    (other caller extras such as ``strata_checkpoint`` are preserved);
    when ``frames`` is ``None`` or empty the raw_mp4 video payload is kept
    unchanged (``payload_mode == "video"``) with the ``num_frames`` hint in
    ``extra_body``.

    ``frame_fingerprint`` is the namespaced checkpoint fingerprint to
    use: image mode passes ``"frames:" + frame_set_fingerprint(...)``
    (frames + policy + source content signature) and it is used
    verbatim — the caller must already have namespaced it; when
    ``None`` the legacy namespaced video value
    ``"video:" + sha256(video_data_url)`` is computed so video-mode
    checkpoints keep today's invalidation semantics with a
    namespaced stored value. The effective value is returned in
    :attr:`SubChunkRunResult.frame_fingerprint`. Supplying ``frames``
    without ``frame_fingerprint`` raises :class:`ValueError`.
    """
    has_frames = frames is not None and len(frames) > 0
    # INVARIANT: the output cap sent here must equal the cap the frame-budget
    # formula subtracted in compute_frame_plan, otherwise `input + output`
    # can exceed `max_model_len` (the plan validated against 4096 while the
    # request asks for 8192). Both sides therefore call the ONE resolver
    # resolve_image_output_tokens (frame_sampling.max_output_tokens ->
    # top-level max_output_tokens -> 4096, clamped by max_model_len); video
    # mode keeps this scene's 8192 default unchanged.
    if has_frames:
        max_output_tokens = resolve_image_output_tokens(provider)
    else:
        max_output_tokens = resolve_max_output_tokens(provider, scene_default=8192)
    if video_data_url is None and not has_frames:
        # Only the raw_mp4 path builds the video data URL. Image mode
        # must never read / base64 the whole video — the frames are the
        # only visual payload it sends.
        video_data_url = build_sub_chunk_video_url(sub_chunk)

    if frame_fingerprint is None:
        if has_frames:
            raise ValueError(
                "execute_sub_chunk requires frame_fingerprint when frames is "
                "supplied (image mode); compute it via "
                "frames_mode_frame_fingerprint"
            )
        frame_fingerprint = video_mode_frame_fingerprint(video_data_url)

    if has_frames:
        # Image mode: frames are sent inline, so no video sampling hint;
        # caller extras (e.g. strata_checkpoint) must survive.
        extra_body = dict(extra_body or {})
        payload_mode = "image"
    elif extra_body is None:
        extra_body = build_sub_chunk_extra_body(
            resolve_num_frames(provider, scene_default=RAW_MP4_NUM_FRAMES)
        )
        payload_mode = "video"
    else:
        payload_mode = "video"

    messages = _build_messages(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        video_data_url=video_data_url,
        frames=frames,
    )

    response_text = client.chat_completion(
        messages=messages,
        temperature=0,
        max_tokens=max_output_tokens,
        response_format={"type": "json_object"},
        extra_body=extra_body,
    )
    raw_response_text = client.get_last_raw_response_text()
    if not response_text:
        raise ValueError(
            "Empty response from vision provider for sub-chunk "
            f"{sub_chunk.chunk_index}-{sub_chunk.sub_chunk_index}"
        )

    if response_parser is None:
        from src.services.video_analysis.output_parser import (
            parse_video_recognition_output,
        )

        recognition_result = parse_video_recognition_output(response_text)
    else:
        recognition_result = response_parser(response_text)
    usage = client.get_last_usage() or {}
    prompt_tokens, completion_tokens, total_tokens = _token_counts_from_usage(usage)

    return SubChunkRunResult(
        recognition_result=recognition_result,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        usage=dict(usage),
        raw_response_text=raw_response_text,
        video_data_url=video_data_url or "",
        extra_body=extra_body,
        payload_mode=payload_mode,
        frame_fingerprint=frame_fingerprint,
    )


# --- checkpoint_writer.py ---
CHECKPOINT_STATE_PENDING = "pending"


# --- checkpoint_writer.py ---
CHECKPOINT_STATE_PROCESSING = "processing"


# --- checkpoint_writer.py ---
CHECKPOINT_STATE_SUCCESS = "success"


# --- checkpoint_writer.py ---
CHECKPOINT_STATE_ERROR = "error"


# --- checkpoint_writer.py ---
RESUMABLE_STATES = frozenset(
    {CHECKPOINT_STATE_PENDING, CHECKPOINT_STATE_PROCESSING, CHECKPOINT_STATE_ERROR}
)


# --- checkpoint_writer.py ---
@dataclass(frozen=True)
class ClaimGuards:
    """Fencing guards captured by :func:`claim.claim_session_for_analysis`.

    The :class:`LateWorkerFencingError` is raised when one of these
    values no longer matches the current TaskLog row at write time.

    ``lease_owner`` is ``str | None`` to mirror
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
    lease_owner: str | None


# --- checkpoint_writer.py ---
@dataclass(frozen=True)
class CheckpointWriteResult:
    """Outcome of :func:`write_sub_chunk_checkpoint`."""

    checkpoint: SessionAnalysisCheckpoint
    written: bool  # True if the UPDATE matched a row; False if the worker is late.


# --- checkpoint_writer.py ---
def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


# --- checkpoint_writer.py ---
def _lookup_checkpoint(
    db: Session,
    *,
    session_id: int,
    analysis_run_id: str,
    chunk_index: int,
    sub_chunk_index: int,
) -> SessionAnalysisCheckpoint | None:
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


# --- checkpoint_writer.py ---
def get_or_create_checkpoint(
    db: Session,
    *,
    session_id: int,
    claim: ClaimGuards,
    sub_chunk: SubChunkPlan,
    system_prompt: str,
    user_prompt: str,
    video_data_url: str | None = None,
    frame_fingerprint: str | None = None,
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

    ``frame_fingerprint`` is the namespaced checkpoint fingerprint to
    store (image mode: ``"frames:" + frame_set_fingerprint(...)``);
    when ``None`` the legacy namespaced video value
    ``"video:" + sha256(video_data_url)`` is derived instead, so
    ``video_data_url`` must be supplied in that case.
    """
    if frame_fingerprint is None:
        if video_data_url is None:
            raise ValueError(
                "get_or_create_checkpoint requires video_data_url when frame_fingerprint "
                "is not supplied"
            )
        frame_fingerprint = video_mode_frame_fingerprint(video_data_url)

    fingerprint = sub_chunk_fingerprint(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        frame_fingerprint=frame_fingerprint,
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
            # ``SessionLocal`` runs with ``autoflush=False``, so the reset
            # above is invisible to the fencing UPDATEs in
            # :func:`mark_checkpoint_processing` / :func:`write_sub_chunk_checkpoint`
            # (they filter on ``input_fingerprint``) until the transaction
            # flushes. Image mode hits this on every file: the pre-sampler
            # claim stored :data:`PROVISIONAL_FRAMES_FINGERPRINT` and this
            # reset rewrites it to the real frame fingerprint. Flushing keeps
            # the row on the same id / analysis_run_id / resumable-state
            # predicates the fencing already enforces; a stale worker still
            # fails :func:`enforce_fencing` and the whole transaction rolls
            # back.
            db.flush()
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


# --- checkpoint_writer.py ---
def claim_checkpoint_before_sampling(
    db: Session,
    *,
    session_id: int,
    claim: ClaimGuards,
    sub_chunk: SubChunkPlan,
) -> SessionAnalysisCheckpoint | None:
    """Image-mode pre-sampler claim: own the checkpoint row before sampling.

    Ensures the sub-chunk's checkpoint row exists (created with
    :data:`PROVISIONAL_FRAMES_FINGERPRINT` when missing) and flips any
    non-``success`` row to ``processing`` (fenced, lease renewed) so a
    sampler failure is fenced to a concrete row: the loop-exception
    handler finds the ``processing`` checkpoint, marks it ``error`` and
    lands the session on ``PARTIAL``.

    Returns ``None`` when the row is already ``success`` — the resume
    decision (skip the LLM) can only be made once the post-sample frame
    fingerprint is known, and the row must not be touched before then.
    """
    row = _lookup_checkpoint(
        db,
        session_id=session_id,
        analysis_run_id=claim.analysis_run_id,
        chunk_index=sub_chunk.chunk_index,
        sub_chunk_index=sub_chunk.sub_chunk_index,
    )
    if row is None:
        row = SessionAnalysisCheckpoint(
            session_id=session_id,
            analysis_run_id=claim.analysis_run_id,
            chunk_index=sub_chunk.chunk_index,
            sub_chunk_index=sub_chunk.sub_chunk_index,
            start_offset_seconds=sub_chunk.start_offset_seconds,
            input_fingerprint=PROVISIONAL_FRAMES_FINGERPRINT,
            state=CHECKPOINT_STATE_PENDING,
        )
        try:
            with db.begin_nested():
                db.add(row)
                db.flush()
        except IntegrityError:
            row = _lookup_checkpoint(
                db,
                session_id=session_id,
                analysis_run_id=claim.analysis_run_id,
                chunk_index=sub_chunk.chunk_index,
                sub_chunk_index=sub_chunk.sub_chunk_index,
            )
            if row is None:
                raise
    if row.state == CHECKPOINT_STATE_SUCCESS:
        return None
    mark_checkpoint_processing(
        db,
        claim=claim,
        checkpoint=row,
        lease_owner=claim.lease_owner or "",
    )
    return row


# --- checkpoint_writer.py ---
def _checkpoint_for_work(
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


# --- checkpoint_writer.py ---
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


# --- checkpoint_writer.py ---
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


# --- checkpoint_writer.py ---
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


# --- checkpoint_failure.py ---
logger = logging.getLogger(__name__)


# --- checkpoint_failure.py ---
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


# --- checkpoint_failure.py ---
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


# --- checkpoint_failure.py ---
def record_recovery_failure(
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
    """Mark the TaskLog as FAILED with the standard analyzer error detail.

    The detail payload always carries ``failure_kind``
    (:func:`src.services.frame_sampling.classify_failure_kind`):
    ``"sampler_unavailable"`` when the failure was a frame-sampler
    acquisition failure, ``"llm"`` for every other failure (vision LLM
    call, output parsing, provider setup). The marker lands on both
    session transitions (``PARTIAL`` when a checkpoint was in flight,
    ``FAILED`` otherwise) because both are finalised through this
    function. Existing ``detail_json`` keys (e.g. ``frame_plan``) are
    preserved — ``finalize_task_log`` merges.
    """
    transition_task_log(
        db,
        claim.task_log_id,
        TaskStatus.RUNNING,
        TaskStatus.FAILED,
        reason="analysis_failed",
        source="analyze_session_task",
    )
    finalize_task_log(
        db,
        task_log,
        TaskStatus.FAILED,
        truncate_text(str(error), 500) or str(error),
        {
            "session_id": session_id,
            "failed_chunk_index": failed_chunk_index,
            "failed_sub_chunk_index": failed_sub_chunk_index,
            "error_type": type(error).__name__,
            "failure_kind": classify_failure_kind(error),
            "prompt_text": truncate_text(last_prompt_text, 4000),
            "raw_response_excerpt": truncate_text(last_response_text, 1500),
            "raw_response_full": truncate_text(last_response_text, 16000),
            "raw_llm_response_excerpt": truncate_text(last_raw_response_text, 1500),
            "raw_llm_response_full": truncate_text(last_raw_response_text, 16000),
            "priority": priority,
        },
    )


__all__ = [
    "CHECKPOINT_STATE_ERROR",
    "CHECKPOINT_STATE_PENDING",
    "CHECKPOINT_STATE_PROCESSING",
    "CHECKPOINT_STATE_SUCCESS",
    "CheckpointWriteResult",
    "ClaimGuards",
    "FRAMES_FINGERPRINT_PREFIX",
    "PROVISIONAL_FRAMES_FINGERPRINT",
    "RESUMABLE_STATES",
    "SubChunkRunResult",
    "VIDEO_FINGERPRINT_PREFIX",
    "_checkpoint_for_work",
    "build_image_content",
    "build_provider_client",
    "build_sub_chunk_extra_body",
    "build_sub_chunk_video_url",
    "claim_checkpoint_before_sampling",
    "enforce_fencing",
    "execute_sub_chunk",
    "frames_mode_frame_fingerprint",
    "get_or_create_checkpoint",
    "mark_checkpoint_error",
    "mark_checkpoint_processing",
    "record_recovery_failure",
    "video_mode_frame_fingerprint",
    "write_sub_chunk_checkpoint",
    "write_token_usage",
]
