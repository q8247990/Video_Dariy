"""Session analysis task: LLM-based video recognition.

Uses dedicated Celery queues (analysis_hot / analysis_full) consumed by the
dedicated celery_vision_worker with --concurrency=1, so globally at most one
session analysis task runs at any moment.
"""

import json
import logging
import time
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any

from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from src.application.prompt.compiler import compile_video_recognition_prompt
from src.application.prompt.contracts import (
    SessionPromptContext,
    StrategyPromptContext,
    VideoRecognitionPromptInput,
    VideoSourcePromptContext,
)
from src.core.celery_app import celery_app
from src.core.config import settings
from src.db.session import task_db_session
from src.infrastructure.llm.openai_gateway import OpenAICompatGatewayFactory
from src.models.event_record import EventRecord
from src.models.llm_provider import LLMProvider
from src.models.session_analysis_checkpoint import SessionAnalysisCheckpoint
from src.models.video_session import VideoSession
from src.models.video_source import VideoSource
from src.services.home_profile import build_home_context
from src.services.llm_output_utils import truncate_text
from src.services.llm_qos import enforce_token_quota, record_token_usage
from src.services.pipeline_constants import (
    SessionAnalysisStatus,
    SourceType,
    TaskStatus,
    TaskType,
)
from src.services.pipeline_state import transition_session, transition_task_log
from src.services.prompt_builder.v2.video_recognition import build_strategy_note
from src.services.provider_key_crypto import decrypt_provider_api_key
from src.services.provider_selector import PROVIDER_TYPE_VISION, find_required_enabled_provider
from src.services.session_analysis_video import (
    SessionVideoChunk,
    SubChunk,
    build_chunk_keyframe_payload,
    build_chunk_sub_chunks,
    build_chunk_video_data_url,
    build_session_video_chunks,
    session_chunk_from_sub_chunk,
)
from src.services.task_dispatch_control import (
    TaskCancellationRequested,
    bind_or_create_running_task_log,
    ensure_task_not_cancelled,
    finalize_cancelled_task_log,
    finalize_task_log,
    get_task_log_for_update,
    renew_task_lease,
)
from src.services.video_analysis.enums import VIDEO_EVENT_TYPES
from src.services.video_analysis.mapper import build_event_record_from_recognized_event
from src.services.video_analysis.output_parser import parse_video_recognition_output
from src.services.video_analysis.schemas import RecognitionResultDTO

logger = logging.getLogger(__name__)
NOT_FOUND_RETRY_DELAYS_SECONDS = (0.5, 1.0, 2.0)
POSTGRES_RETRYABLE_SQLSTATES = {"40P01", "40001"}
DEADLOCK_MAX_RETRIES = 3


def _claim_session_for_analysis(
    db: Session, session_id: int
) -> tuple[VideoSession | None, str | None]:
    attempts = (0.0,) + NOT_FOUND_RETRY_DELAYS_SECONDS
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
            for from_status in (SessionAnalysisStatus.SEALED, SessionAnalysisStatus.PARTIAL)
        )
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


def _skip_analysis_task(
    db: Session,
    task_log: Any,
    session_id: int,
    reason: str,
    priority: str,
) -> dict[str, Any]:
    reason_messages = {
        "not_found": f"Skipped session {session_id}, not found",
        "already_analyzing": f"Skipped session {session_id}, already analyzing",
        "session_open": f"Skipped session {session_id}, session still open",
    }
    current_status = None
    if reason.startswith("status_"):
        current_status = reason.removeprefix("status_")
    message = reason_messages.get(reason)
    if message is None:
        status_text = current_status or reason
        message = f"Skipped session {session_id}, current status is {status_text}"

    detail = {
        "session_id": session_id,
        "skipped": True,
        "reason": reason,
        "priority": priority,
    }
    if current_status is not None:
        detail["current_status"] = current_status

    transition_task_log(
        db,
        task_log.id,
        TaskStatus.RUNNING,
        TaskStatus.SKIPPED,
        reason="analysis_skipped",
        source="skip_analysis_task",
    )
    finalize_task_log(task_log, TaskStatus.SKIPPED, message, detail)
    db.commit()
    return {"events_created": 0, "skipped": True, "reason": reason}


def _is_deadlock_operational_error(exc: Exception) -> bool:
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


def _mark_session_sealed_for_retry(db: Session, session_id: int) -> None:
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


def _resolve_ingest_type(source: VideoSource) -> str:
    if source.source_type == SourceType.LOCAL_DIRECTORY:
        return "xiaomi_nas_backup"
    return source.source_type


def _build_prompts(
    source: VideoSource,
    home_context: dict[str, Any],
    session: VideoSession,
    chunk: SessionVideoChunk | SubChunk,
) -> tuple[str, str]:
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
                segment_index=chunk.chunk_index,
                segment_start_offset_sec=chunk.start_offset_seconds,
                segment_duration_seconds=chunk.duration_seconds,
            ),
            strategy_context=StrategyPromptContext(
                ingest_type=ingest_type,
                source_type=source.source_type,
                strategy_note=strategy_note,
            ),
            event_type_list=sorted(VIDEO_EVENT_TYPES),
        )
    )


def _aggregate_session_fields(
    session: VideoSession,
    structured_results: list[tuple[int, int, RecognitionResultDTO]],
    events: list[EventRecord],
) -> None:
    if not structured_results:
        session.summary_text = (
            f"分段识别完成，共识别 {len(events)} 个事件" if events else "未识别到有效事件"
        )
        session.activity_level = "medium" if events else "low"
        session.main_subjects_json = []
        session.has_important_event = any(
            event.importance_level in {"high", "medium"} for event in events
        )
        session.analysis_notes_json = []
        return

    summary_lines: list[str] = []
    if len(structured_results) == 1:
        summary_lines.append(structured_results[0][2].session_summary.summary_text)
    else:
        for chunk_index, sub_index, result in structured_results:
            summary_lines.append(
                f"片段{chunk_index + 1}-{sub_index + 1}: {result.session_summary.summary_text}"
            )

    activity_score = {"low": 1, "medium": 2, "high": 3}
    highest_activity = "low"
    subjects_seen: set[str] = set()
    merged_subjects: list[str] = []
    merged_notes: list[dict[str, str]] = []
    notes_seen: set[tuple[str, str]] = set()
    has_important_event = any(event.importance_level in {"high", "medium"} for event in events)

    for _, _, result in structured_results:
        summary = result.session_summary
        if activity_score[summary.activity_level] > activity_score[highest_activity]:
            highest_activity = summary.activity_level
        has_important_event = has_important_event or summary.has_important_event

        for subject in summary.main_subjects:
            key = subject.strip()
            if not key or key in subjects_seen:
                continue
            subjects_seen.add(key)
            merged_subjects.append(key)

        for note in result.analysis_notes:
            note_key = (note.type, note.note.strip())
            if not note_key[1] or note_key in notes_seen:
                continue
            notes_seen.add(note_key)
            merged_notes.append({"type": note.type, "note": note.note})

    session.summary_text = "\n".join(summary_lines)
    session.activity_level = highest_activity
    session.main_subjects_json = merged_subjects
    session.has_important_event = has_important_event
    session.analysis_notes_json = merged_notes


def _replace_session_events(db: Session, session_id: int, events: list[EventRecord]) -> int:
    deleted_count = (
        db.query(EventRecord)
        .filter(EventRecord.session_id == session_id)
        .delete(synchronize_session=False)
    )
    for event in events:
        db.add(event)
    return int(deleted_count or 0)


def _build_provider_client(db: Session) -> tuple[Any, LLMProvider]:
    provider = find_required_enabled_provider(db, PROVIDER_TYPE_VISION)
    client = OpenAICompatGatewayFactory().build(
        api_base_url=provider.api_base_url,
        api_key=decrypt_provider_api_key(provider.api_key),
        model_name=provider.model_name,
        timeout_seconds=provider.timeout_seconds,
    )
    return client, provider


def _analysis_run_id(chunks: list[SessionVideoChunk]) -> str:
    identities: list[dict[str, Any]] = []
    for chunk in chunks:
        for path in chunk.file_paths:
            path_info: dict[str, Any] = {"path": path}
            if Path(path).exists():
                stat = Path(path).stat()
                path_info.update({"size": stat.st_size, "mtime_ns": stat.st_mtime_ns})
            identities.append(path_info)
    encoded = json.dumps(identities, sort_keys=True, separators=(",", ":")).encode()
    return sha256(encoded).hexdigest()


def _sub_chunk_fingerprint(
    system_prompt: str,
    user_prompt: str,
    video_data_url: str,
    sub_chunk: SubChunk,
) -> str:
    payload = {
        "system_prompt": system_prompt,
        "user_prompt": user_prompt,
        "video_sha256": sha256(video_data_url.encode()).hexdigest(),
        "start_offset_seconds": sub_chunk.start_offset_seconds,
        "duration_seconds": sub_chunk.duration_seconds,
        "file_paths": sub_chunk.file_paths,
    }
    return sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _checkpoint_for_work(
    db: Session,
    *,
    session_id: int,
    analysis_run_id: str,
    chunk_index: int,
    sub_chunk: SubChunk,
    input_fingerprint: str,
) -> SessionAnalysisCheckpoint:
    checkpoint = (
        db.query(SessionAnalysisCheckpoint)
        .filter(
            SessionAnalysisCheckpoint.session_id == session_id,
            SessionAnalysisCheckpoint.analysis_run_id == analysis_run_id,
            SessionAnalysisCheckpoint.chunk_index == chunk_index,
            SessionAnalysisCheckpoint.sub_chunk_index == sub_chunk.sub_chunk_index,
        )
        .first()
    )
    if checkpoint is not None:
        if checkpoint.input_fingerprint != input_fingerprint:
            checkpoint.state = "pending"
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
        sub_chunk_index=sub_chunk.sub_chunk_index,
        start_offset_seconds=sub_chunk.start_offset_seconds,
        input_fingerprint=input_fingerprint,
        state="pending",
    )
    try:
        with db.begin_nested():
            db.add(checkpoint)
            db.flush()
    except IntegrityError:
        checkpoint = (
            db.query(SessionAnalysisCheckpoint)
            .filter(
                SessionAnalysisCheckpoint.session_id == session_id,
                SessionAnalysisCheckpoint.analysis_run_id == analysis_run_id,
                SessionAnalysisCheckpoint.chunk_index == chunk_index,
                SessionAnalysisCheckpoint.sub_chunk_index == sub_chunk.sub_chunk_index,
            )
            .one()
        )
    return checkpoint


def _completed_results(
    db: Session, session: VideoSession, analysis_run_id: str
) -> tuple[list[tuple[int, int, RecognitionResultDTO]], list[EventRecord]]:
    checkpoints = (
        db.query(SessionAnalysisCheckpoint)
        .filter(
            SessionAnalysisCheckpoint.session_id == session.id,
            SessionAnalysisCheckpoint.analysis_run_id == analysis_run_id,
            SessionAnalysisCheckpoint.state == "success",
        )
        .order_by(SessionAnalysisCheckpoint.chunk_index, SessionAnalysisCheckpoint.sub_chunk_index)
        .all()
    )
    results: list[tuple[int, int, RecognitionResultDTO]] = []
    events: list[EventRecord] = []
    for checkpoint in checkpoints:
        if checkpoint.event_payload is None:
            continue
        result = RecognitionResultDTO.model_validate(checkpoint.event_payload)
        results.append((checkpoint.chunk_index, checkpoint.sub_chunk_index, result))
        for item in result.events:
            events.append(
                build_event_record_from_recognized_event(
                    session, item, base_offset_seconds=checkpoint.start_offset_seconds
                )
            )
    return results, events


@celery_app.task(bind=True, max_retries=DEADLOCK_MAX_RETRIES)
def analyze_session_task(self, session_id: int, priority: str = "hot") -> dict:  # noqa: C901
    """Analyze a sealed session using LLM vision.

    Dispatched to the analysis_hot or analysis_full queue by the caller and
    consumed by the dedicated celery_vision_worker (--concurrency=1).
    The worker process runs at most one session analysis task at a time.
    """
    with task_db_session() as db:
        session = None
        last_chunk_index: int | None = None
        last_sub_chunk_index: int | None = None
        last_prompt_text: str | None = None
        last_response_text: str | None = None
        last_raw_response_text: str | None = None
        current_checkpoint: SessionAnalysisCheckpoint | None = None
        queue_task_id = str(getattr(getattr(self, "request", None), "id", "") or "")
        task_log = bind_or_create_running_task_log(
            db,
            queue_task_id=queue_task_id or None,
            task_type=TaskType.SESSION_ANALYSIS,
            task_target_id=session_id,
            detail_json={"priority": priority},
        )
        db.commit()

        try:
            ensure_task_not_cancelled(
                db, task_log.id, default_message=f"Analysis cancelled for session {session_id}"
            )
            session, skip_reason = _claim_session_for_analysis(db, session_id)
            if skip_reason is not None:
                return _skip_analysis_task(db, task_log, session_id, skip_reason, priority)
            if session is None:
                return _skip_analysis_task(db, task_log, session_id, "not_found", priority)

            source = db.query(VideoSource).filter(VideoSource.id == session.source_id).first()
            if not source:
                raise ValueError(f"Video source {session.source_id} not found")

            home_context = build_home_context(db)
            chunks = build_session_video_chunks(
                db,
                session.id,
                chunk_seconds=settings.ANALYZER_SEGMENT_SECONDS,
            )
            analysis_run_id = _analysis_run_id(chunks)
            client, provider = _build_provider_client(db)
            configured_mode = (
                (getattr(provider, "video_preprocess_mode", "raw_mp4") or "raw_mp4").strip().lower()
            )
            if configured_mode != "raw_mp4":
                logger.warning(
                    "Provider %s has video_preprocess_mode=%r, but the keyframe "
                    "mode is disabled; forcing raw_mp4",
                    provider.id,
                    configured_mode,
                )
            # The keyframe path below is kept intentionally, but it is not
            # reachable: this pipeline always sends raw mp4 to the vision model.
            preprocess_mode = "raw_mp4"
            keyframe_target_n = int(getattr(provider, "video_keyframe_target_n", 64) or 64)
            keyframe_jpeg_quality = int(getattr(provider, "video_keyframe_jpeg_quality", 88) or 88)
            fallback_to_mp4 = settings.ANALYZER_VIDEO_KEYFRAME_FALLBACK_TO_MP4

            parse_modes: list[str] = []
            events_to_persist: list[EventRecord] = []
            structured_results: list[tuple[int, int, RecognitionResultDTO]] = []
            sub_chunk_count = 0
            keyframe_fallback_count = 0
            keyframe_total_count = 0
            for chunk in chunks:
                ensure_task_not_cancelled(
                    db,
                    task_log.id,
                    default_message=f"Analysis cancelled for session {session_id}",
                )
                last_chunk_index = chunk.chunk_index
                sub_chunks = build_chunk_sub_chunks(
                    chunk,
                    db,
                    sub_chunk_seconds=settings.ANALYZER_LLM_CHUNK_SECONDS,
                )
                for sub_chunk in sub_chunks:
                    ensure_task_not_cancelled(
                        db,
                        task_log.id,
                        default_message=(
                            f"Analysis cancelled for session {session_id}, "
                            f"chunk {chunk.chunk_index}-{sub_chunk.sub_chunk_index}"
                        ),
                    )
                    last_chunk_index = chunk.chunk_index
                    last_sub_chunk_index = sub_chunk.sub_chunk_index

                    effective_mode = preprocess_mode
                    extra_body: dict[str, Any] | None = None
                    video_part: dict[str, Any]

                    if effective_mode == "keyframe":
                        try:
                            payload = build_chunk_keyframe_payload(
                                sub_chunk,
                                target_n=keyframe_target_n,
                                jpeg_quality=keyframe_jpeg_quality,
                                mad_threshold=settings.ANALYZER_VIDEO_KEYFRAME_MAD_THRESHOLD,
                                phash_threshold=settings.ANALYZER_VIDEO_KEYFRAME_PHASH_THRESHOLD,
                                periodic_anchor_seconds=settings.ANALYZER_VIDEO_KEYFRAME_PERIOD_SECONDS,
                            )
                            keyframe_total_count += 1
                            extra_body = {"media_io_kwargs": payload.media_io_kwargs}
                            video_part = {
                                "type": "video_url",
                                "video_url": {"url": payload.jpeg_data_url},
                            }
                        except Exception as exc:
                            if not fallback_to_mp4:
                                logger.exception(
                                    "keyframe extraction failed for session %s "
                                    "chunk %s-%s; fallback disabled",
                                    session_id,
                                    chunk.chunk_index,
                                    sub_chunk.sub_chunk_index,
                                )
                                raise
                            logger.warning(
                                "keyframe extraction failed for session %s "
                                "chunk %s-%s, falling back to raw_mp4: %s",
                                session_id,
                                chunk.chunk_index,
                                sub_chunk.sub_chunk_index,
                                exc,
                            )
                            keyframe_fallback_count += 1
                            effective_mode = "raw_mp4"

                    if effective_mode == "raw_mp4":
                        fallback_chunk = session_chunk_from_sub_chunk(
                            sub_chunk, parent_chunk_index=chunk.chunk_index
                        )
                        video_data_url = build_chunk_video_data_url(fallback_chunk)
                        extra_body = {
                            "media_io_kwargs": {
                                "video": {
                                    "num_frames": keyframe_target_n,
                                }
                            }
                        }
                        video_part = {
                            "type": "video_url",
                            "video_url": {"url": video_data_url},
                        }

                    system_prompt, user_prompt = _build_prompts(
                        source, home_context, session, sub_chunk
                    )
                    last_prompt_text = user_prompt
                    input_fingerprint = _sub_chunk_fingerprint(
                        system_prompt, user_prompt, video_data_url, sub_chunk
                    )
                    current_checkpoint = _checkpoint_for_work(
                        db,
                        session_id=session.id,
                        analysis_run_id=analysis_run_id,
                        chunk_index=chunk.chunk_index,
                        sub_chunk=sub_chunk,
                        input_fingerprint=input_fingerprint,
                    )
                    if current_checkpoint.state == "success":
                        continue
                    current_checkpoint.state = "processing"
                    current_checkpoint.attempt_count += 1
                    renew_task_lease(db, task_log.id, queue_task_id or None)
                    db.commit()
                    enforce_token_quota(db, provider)

                    response_text = client.chat_completion(
                        messages=[
                            {"role": "system", "content": system_prompt},
                            {
                                "role": "user",
                                "content": [
                                    video_part,
                                    {"type": "text", "text": user_prompt},
                                ],
                            },
                        ],
                        temperature=0,
                        max_tokens=8192,
                        response_format={"type": "json_object"},
                        extra_body=extra_body,
                    )
                    last_response_text = response_text
                    last_raw_response_text = client.get_last_raw_response_text()
                    if not response_text:
                        raise ValueError(
                            f"Empty response from vision provider for chunk "
                            f"{chunk.chunk_index}-{sub_chunk.sub_chunk_index}"
                        )

                    recognition_result = parse_video_recognition_output(response_text)
                    usage = client.get_last_usage() or {}
                    prompt_tokens = int(usage.get("prompt_tokens") or 0)
                    completion_tokens = int(usage.get("completion_tokens") or 0)
                    total_tokens = int(usage.get("total_tokens") or 0)
                    if total_tokens <= 0:
                        total_tokens = prompt_tokens + completion_tokens
                    current_checkpoint.event_payload = recognition_result.model_dump(mode="json")
                    current_checkpoint.prompt_tokens = prompt_tokens
                    current_checkpoint.completion_tokens = completion_tokens
                    current_checkpoint.total_tokens = total_tokens
                    current_checkpoint.state = "success"
                    current_checkpoint.completed_at = datetime.now(timezone.utc)
                    record_token_usage(
                        db,
                        provider_id=provider.id,
                        provider_name_snapshot=provider.provider_name,
                        scene="video_analysis",
                        usage=usage,
                        session_id=session.id,
                        analysis_checkpoint_id=current_checkpoint.id,
                    )
                    renew_task_lease(db, task_log.id, queue_task_id or None)
                    db.commit()
                    parse_modes.append("new")
                    sub_chunk_count += 1

            ensure_task_not_cancelled(
                db, task_log.id, default_message=f"Analysis cancelled for session {session_id}"
            )
            structured_results, events_to_persist = _completed_results(db, session, analysis_run_id)
            _aggregate_session_fields(session, structured_results, events_to_persist)
            replaced_deleted_count = _replace_session_events(db, session_id, events_to_persist)

            completed = transition_session(
                db,
                session.id,
                SessionAnalysisStatus.ANALYZING,
                SessionAnalysisStatus.SUCCESS,
                reason="analysis_completed",
                source="analyze_session_task",
                task_log=task_log,
            )
            if not completed.applied:
                return _skip_analysis_task(
                    db, task_log, session_id, "completion_transition_conflict", priority
                )
            session.last_analyzed_at = datetime.now(timezone.utc)

            transition_task_log(
                db,
                task_log.id,
                TaskStatus.RUNNING,
                TaskStatus.SUCCESS,
                reason="analysis_completed",
                source="analyze_session_task",
            )
            finalize_task_log(
                task_log,
                TaskStatus.SUCCESS,
                f"Analyzed session in {len(chunks)} chunks / "
                f"{sub_chunk_count} sub-chunks, created {len(events_to_persist)} events.",
                {
                    "session_id": session_id,
                    "events_created": len(events_to_persist),
                    "events_replaced_deleted": replaced_deleted_count,
                    "chunk_count": len(chunks),
                    "sub_chunk_count": sub_chunk_count,
                    "chunk_seconds": settings.ANALYZER_SEGMENT_SECONDS,
                    "llm_chunk_seconds": settings.ANALYZER_LLM_CHUNK_SECONDS,
                    "preprocess_mode": preprocess_mode,
                    "keyframe_total": keyframe_total_count,
                    "keyframe_fallback": keyframe_fallback_count,
                    "parse_modes": parse_modes,
                    "priority": priority,
                },
            )
            db.commit()
            return {
                "events_created": len(events_to_persist),
                "chunk_count": len(chunks),
                "sub_chunk_count": sub_chunk_count,
            }

        except TaskCancellationRequested as exc:
            logger.info("Analysis task cancelled for session %s", session_id)
            db.rollback()
            refreshed_task_log = get_task_log_for_update(db, task_log.id)
            if refreshed_task_log is None:
                raise

            refreshed_session = db.query(VideoSession).filter(VideoSession.id == session_id).first()
            if (
                refreshed_session is not None
                and refreshed_session.analysis_status == SessionAnalysisStatus.ANALYZING
            ):
                transition_session(
                    db,
                    refreshed_session.id,
                    SessionAnalysisStatus.ANALYZING,
                    SessionAnalysisStatus.SEALED,
                    reason="analysis_cancelled",
                    source="analyze_session_task",
                    task_log=refreshed_task_log,
                )

            transition_task_log(
                db,
                refreshed_task_log.id,
                TaskStatus.RUNNING,
                TaskStatus.CANCELLED,
                reason="analysis_cancelled",
                source="analyze_session_task",
            )
            finalize_cancelled_task_log(
                refreshed_task_log,
                str(exc),
                {
                    "session_id": session_id,
                    "cancelled": True,
                    "failed_chunk_index": last_chunk_index,
                    "failed_sub_chunk_index": last_sub_chunk_index,
                    "priority": priority,
                },
            )
            db.commit()
            return {
                "cancelled": True,
                "session_id": session_id,
                "chunk_index": last_chunk_index,
                "sub_chunk_index": last_sub_chunk_index,
            }

        except Exception as e:
            logger.exception(
                "Failed to analyze session %s, chunk=%s, prompt=%r, raw_response=%r",
                session_id,
                last_chunk_index,
                truncate_text(last_prompt_text, 4000),
                truncate_text(last_raw_response_text, 16000)
                or truncate_text(last_response_text, 16000),
            )
            db.rollback()

            if current_checkpoint is not None:
                current_checkpoint = db.merge(current_checkpoint)
                current_checkpoint.state = "error"
                current_checkpoint.last_error = truncate_text(str(e), 500) or str(e)
                current_checkpoint.error_type = type(e).__name__
                db.commit()

            if _is_deadlock_operational_error(e) and self.request.retries < DEADLOCK_MAX_RETRIES:
                countdown = 2**self.request.retries
                _mark_session_sealed_for_retry(db, session_id)
                task_log.retry_count = self.request.retries + 1
                task_log.message = (
                    f"Deadlock detected, retry {self.request.retries + 1}/"
                    f"{DEADLOCK_MAX_RETRIES} in {countdown}s"
                )
                db.commit()
                raise self.retry(exc=e, countdown=countdown) from e

            transition_task_log(
                db,
                task_log.id,
                TaskStatus.RUNNING,
                TaskStatus.FAILED,
                reason="analysis_failed",
                source="analyze_session_task",
            )
            finalize_task_log(
                task_log,
                TaskStatus.FAILED,
                truncate_text(str(e), 500) or str(e),
                {
                    "session_id": session_id,
                    "failed_chunk_index": last_chunk_index,
                    "failed_sub_chunk_index": last_sub_chunk_index,
                    "error_type": type(e).__name__,
                    "prompt_text": truncate_text(last_prompt_text, 4000),
                    "raw_response_excerpt": truncate_text(last_response_text, 1500),
                    "raw_response_full": truncate_text(last_response_text, 16000),
                    "raw_llm_response_excerpt": truncate_text(last_raw_response_text, 1500),
                    "raw_llm_response_full": truncate_text(last_raw_response_text, 16000),
                    "priority": priority,
                },
            )
            if session:
                failed_status = (
                    SessionAnalysisStatus.PARTIAL
                    if current_checkpoint is not None
                    else SessionAnalysisStatus.FAILED
                )
                transition_session(
                    db,
                    session.id,
                    SessionAnalysisStatus.ANALYZING,
                    failed_status,
                    reason="analysis_failed",
                    source="analyze_session_task",
                    task_log=task_log,
                )
            db.commit()
            raise
