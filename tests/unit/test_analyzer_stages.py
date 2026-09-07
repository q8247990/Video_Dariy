"""Unit tests for the analysis-stage modules split out in Wave 5 (Todo 18).

白盒测试：直接调用内部 stage 函数/类，断言绑定实现细节，随实现重构，不作为接口契约回归基线。

These tests exercise the public API of
:mod:`src.services.analysis` end-to-end against the PG test schema
(``tests/conftest.py`` ``pg_db`` fixture) — every model is already
created by ``alembic upgrade head``, so each test only seeds the
rows it actually exercises.

They cover the contracts that the slim Celery task and the
final aggregator / finalize stages depend on:

* :func:`chunk_plan.build_chunk_plan` — the deterministic
  ``analysis_run_id`` / per-sub-chunk fingerprint contract.
* :func:`sub_chunk_runner.build_sub_chunk_video_url`` /
  :func:`sub_chunk_runner.build_sub_chunk_extra_body` — the
  raw_mp4-only payload contract and the
  ``media_io_kwargs.video.num_frames`` setting.
* :func:`video_analysis.output_parser.parse_video_recognition_output`
  — the LLM-output -> DTO contract.
* :func:`checkpoint_writer.write_sub_chunk_checkpoint` /
  :func:`checkpoint_failure.enforce_fencing` — the late-worker fencing
  guards (matching ``analysis_run_id`` / ``generation`` /
  ``lease_owner`` succeeds; a stale worker is rejected with
  :class:`LateWorkerFencingError`).
* :func:`checkpoint_failure.mark_checkpoint_error` /
  :func:`checkpoint_failure.record_recovery_failure` — the failure
  path that flips a checkpoint to ``state=error`` and finalises the
  TaskLog as ``FAILED``.
* :func:`checkpoint_writer.get_or_create_checkpoint` — the resumed
  run skips sub-chunks that are already ``state=success`` (no
  re-charge).

The PostgreSQL-specific concurrency / deadlock / connection-pooling
tests live in ``tests/integration/test_analyzer_stages_postgres.py``.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from src.models.event_record import EventRecord
from src.models.pipeline_transition_log import PipelineTransitionLog
from src.models.session_analysis_checkpoint import SessionAnalysisCheckpoint
from src.models.task_log import TaskLog
from src.models.video_file import VideoFile
from src.models.video_session import VideoSession
from src.models.video_session_file_rel import VideoSessionFileRel
from src.models.video_source import VideoSource
from src.services.analysis import (
    RAW_MP4_NUM_FRAMES,
    ClaimGuards,
    LateWorkerFencingError,
    build_chunk_plan,
    build_sub_chunk_extra_body,
    build_sub_chunk_video_url,
    get_or_create_checkpoint,
    mark_checkpoint_error,
    record_recovery_failure,
    sub_chunk_fingerprint,
    write_sub_chunk_checkpoint,
    write_token_usage,
)
from src.services.analysis.chunk_plan import SubChunkPlan
from src.services.pipeline_constants import TaskStatus
from src.services.task_dispatch_control import finalize_task_log
from src.services.video_analysis.output_parser import parse_video_recognition_output
from src.services.video_analysis.schemas import (
    RecognitionResultDTO,
    RecognizedEventDTO,
    SessionSummaryDTO,
)

# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------


def _seed_source_with_video_files(
    db: Session,
    *,
    file_count: int = 3,
    file_duration_seconds: int = 60,
) -> tuple[int, int]:
    """Seed a VideoSource, a sealed VideoSession, and ``file_count`` VideoFile rows.

    Returns ``(source_id, session_id)``. The VideoFiles are linked to the
    session via ``VideoSessionFileRel`` rows in ``sort_index`` order so
    the chunk / sub-chunk planner produces a deterministic plan.
    """
    source = VideoSource(
        source_name="客厅",
        camera_name="cam1",
        location_name="客厅",
        source_type="local_directory",
        config_json={"root_path": "/tmp"},
        enabled=True,
    )
    db.add(source)
    db.flush()

    base = datetime(2026, 3, 15, 10, 0, 0)
    total = file_count * file_duration_seconds
    session = VideoSession(
        source_id=source.id,
        session_start_time=base,
        session_end_time=base + timedelta(seconds=total),
        total_duration_seconds=total,
        analysis_status="sealed",
    )
    db.add(session)
    db.flush()

    file_ids: list[int] = []
    for index in range(file_count):
        vf = VideoFile(
            source_id=source.id,
            file_name=f"clip-{index:04d}.mp4",
            file_path=f"/tmp/fake/clip-{index:04d}.mp4",
            storage_type="local_file",
            file_format="mp4",
            start_time=base + timedelta(seconds=index * file_duration_seconds),
            end_time=base + timedelta(seconds=(index + 1) * file_duration_seconds),
            duration_seconds=file_duration_seconds,
            parse_status="parsed",
        )
        db.add(vf)
        db.flush()
        file_ids.append(vf.id)
        db.add(
            VideoSessionFileRel(
                session_id=session.id,
                video_file_id=vf.id,
                sort_index=index,
            )
        )
    db.commit()
    return source.id, session.id


def _bind_running_task_log(
    db: Session,
    *,
    session_id: int,
    queue_task_id: str,
) -> TaskLog:
    """Insert + commit a RUNNING ``TaskLog`` that mirrors the claim stage."""
    task_log = TaskLog(
        task_type="session_analysis",
        task_target_id=session_id,
        queue_task_id=queue_task_id,
        dedupe_key=f"session_analysis|{session_id}",
        status=TaskStatus.RUNNING,
        lease_owner=queue_task_id,
        detail_json={
            "priority": "hot",
            "generation": 0,
            "analysis_run_id": "test-run",
        },
    )
    db.add(task_log)
    db.commit()
    db.refresh(task_log)
    return task_log


def _recognition_result(index: int) -> RecognitionResultDTO:
    """Build a minimal ``RecognitionResultDTO`` for ``index``."""
    return RecognitionResultDTO(
        session_summary=SessionSummaryDTO(
            summary_text=f"summary-{index}",
            activity_level="medium",
            main_subjects=["爸爸"],
            has_important_event=True,
        ),
        events=[
            RecognizedEventDTO(
                offset_start_sec=1.0,
                offset_end_sec=5.0,
                event_type="member_appear",
                title=f"event-{index}",
                summary=f"event-{index}",
                detail=f"event-{index}",
                related_entities=[],
                observed_actions=[],
                interpreted_state=[],
                confidence=0.9,
                importance_level="medium",
            )
        ],
        analysis_notes=[],
    )


def _build_guards(task_log: TaskLog) -> ClaimGuards:
    """Capture ClaimGuards from the seeded ``TaskLog``.

    Mirrors what :func:`claim_session_for_analysis` returns after a
    successful claim (TaskLog row carries ``analysis_run_id`` /
    ``generation`` in its ``detail_json`` and ``lease_owner`` on the
    column).
    """
    detail = task_log.detail_json if isinstance(task_log.detail_json, dict) else {}
    return ClaimGuards(
        task_log_id=task_log.id,
        analysis_run_id=str(detail.get("analysis_run_id") or "test-run"),
        generation=int(detail.get("generation") or 0),
        lease_owner=task_log.lease_owner,
    )


# ---------------------------------------------------------------------------
# chunk_plan
# ---------------------------------------------------------------------------


def test_chunk_plan_computes_expected_offsets_and_fingerprints(pg_db: Session) -> None:
    """``build_chunk_plan`` produces the deterministic offsets / per-sub-chunk
    ``analysis_run_id`` / ``input_fingerprint`` contract that the
    checkpoint writer relies on.

    Three 60-second files / one chunk / 60-second sub-chunks produces a
    single chunk with three sub-chunks; the plan's analysis_run_id must
    be stable across repeated calls and the per-sub-chunk fingerprint
    must change when the input changes.
    """
    _, session_id = _seed_source_with_video_files(
        pg_db, file_count=3, file_duration_seconds=60
    )

    plan_first = build_chunk_plan(
        pg_db,
        session_id=session_id,
        chunk_seconds=600,
        sub_chunk_seconds=60,
    )
    plan_second = build_chunk_plan(
        pg_db,
        session_id=session_id,
        chunk_seconds=600,
        sub_chunk_seconds=60,
    )

    assert plan_first.session_id == session_id
    assert plan_first.analysis_run_id == plan_second.analysis_run_id
    assert len(plan_first.chunks) == 1
    assert plan_first.chunks[0].start_offset_seconds == 0
    assert plan_first.chunks[0].duration_seconds == 180
    assert len(plan_first.sub_chunks) == 3
    assert [sc.start_offset_seconds for sc in plan_first.sub_chunks] == [0, 60, 120]
    assert [sc.duration_seconds for sc in plan_first.sub_chunks] == [60, 60, 60]
    assert [sc.chunk_index for sc in plan_first.sub_chunks] == [0, 0, 0]

    plan_item = plan_first.sub_chunks[0]
    fingerprint_a = sub_chunk_fingerprint(
        system_prompt="sys",
        user_prompt="user",
        video_data_url="data:video/mp4;base64,AAA",
        sub_chunk=plan_item,
    )
    fingerprint_b = sub_chunk_fingerprint(
        system_prompt="sys",
        user_prompt="user",
        video_data_url="data:video/mp4;base64,BBB",
        sub_chunk=plan_item,
    )
    assert fingerprint_a != fingerprint_b
    assert len(fingerprint_a) == 64


# ---------------------------------------------------------------------------
# sub_chunk_runner — raw_mp4 payload contract
# ---------------------------------------------------------------------------


def test_single_sub_chunk_builds_raw_mp4_payload_with_120_frames(tmp_path) -> None:
    """The sub-chunk runner emits a ``data:video/mp4;base64,...`` URL and a
    ``media_io_kwargs.video.num_frames == RAW_MP4_NUM_FRAMES`` (== 120).

    Pinning both halves of the raw_mp4-only contract in one place so
    the keyframe re-introduction regression test is single-source.
    """
    fake_mp4 = tmp_path / "fake.mp4"
    fake_mp4.write_bytes(b"\x00\x01\x02\x03")
    plan_item = SubChunkPlan(
        chunk_index=0,
        sub_chunk_index=0,
        start_offset_seconds=0,
        duration_seconds=60,
        file_paths=(str(fake_mp4),),
    )

    url = build_sub_chunk_video_url(plan_chunk_index=0, sub_chunk=plan_item)
    extra_body = build_sub_chunk_extra_body()

    assert url.startswith("data:video/mp4;base64,")
    import base64 as _b64

    assert _b64.b64decode(url.split(",", 1)[1]) == b"\x00\x01\x02\x03"
    assert RAW_MP4_NUM_FRAMES == 120
    assert extra_body == {"media_io_kwargs": {"video": {"num_frames": 120}}}


# ---------------------------------------------------------------------------
# video_analysis.output_parser — raw LLM output -> DTO
# ---------------------------------------------------------------------------


def test_single_sub_chunk_parses_llm_output_into_dto() -> None:
    """A minimal but realistic LLM JSON payload parses into a
    ``RecognitionResultDTO`` carrying one ``RecognizedEventDTO``.

    Pins the public contract that ``sub_chunk_runner.execute_sub_chunk``
    consumes. If the schema ever drifts (a new required field, an enum
    tightening) this test fails before the orchestration does.
    """
    raw = json.dumps(
        {
            "session_summary": {
                "summary_text": "客厅有人走过",
                "activity_level": "medium",
                "main_subjects": ["爸爸"],
                "has_important_event": True,
            },
            "events": [
                {
                    "offset_start_sec": 0.5,
                    "offset_end_sec": 3.5,
                    "event_type": "member_appear",
                    "title": "成员出现",
                    "summary": "爸爸进入客厅",
                    "detail": "10:00:01 爸爸从门口进入客厅",
                    "related_entities": [],
                    "observed_actions": ["walking"],
                    "interpreted_state": ["standing"],
                    "confidence": 0.92,
                    "importance_level": "medium",
                }
            ],
            "analysis_notes": [],
        }
    )

    parsed = parse_video_recognition_output(raw)

    assert isinstance(parsed, RecognitionResultDTO)
    assert parsed.session_summary.summary_text == "客厅有人走过"
    assert parsed.session_summary.activity_level == "medium"
    assert parsed.session_summary.has_important_event is True
    assert len(parsed.events) == 1
    event = parsed.events[0]
    assert isinstance(event, RecognizedEventDTO)
    assert event.event_type == "member_appear"
    assert event.title == "成员出现"
    assert event.offset_start_sec == 0.5
    assert event.offset_end_sec == 3.5
    assert event.confidence == pytest.approx(0.92)


# ---------------------------------------------------------------------------
# checkpoint_writer — fencing guards
# ---------------------------------------------------------------------------


def test_checkpoint_write_requires_matching_run_id_and_generation(
    pg_db: Session,
) -> None:
    """A checkpoint write with matching ``analysis_run_id`` +
    ``generation`` + ``lease_owner`` succeeds and persists the
    success row."""
    _, session_id = _seed_source_with_video_files(
        pg_db, file_count=3, file_duration_seconds=60
    )
    task_log = _bind_running_task_log(pg_db, session_id=session_id, queue_task_id="q-1")
    guards = _build_guards(task_log)

    sub_chunk = SubChunkPlan(
        chunk_index=0,
        sub_chunk_index=0,
        start_offset_seconds=0,
        duration_seconds=60,
        file_paths=("/tmp/fake/clip-0000.mp4",),
    )
    checkpoint = get_or_create_checkpoint(
        pg_db,
        session_id=session_id,
        claim=guards,
        sub_chunk=sub_chunk,
        system_prompt="sys",
        user_prompt="user",
        video_data_url="data:video/mp4;base64,AAA",
    )
    assert checkpoint.state == "pending"
    pg_db.commit()

    write_sub_chunk_checkpoint(
        pg_db,
        claim=guards,
        checkpoint=checkpoint,
        recognition_result=_recognition_result(0),
        prompt_tokens=10,
        completion_tokens=5,
        total_tokens=15,
    )
    pg_db.commit()

    refreshed = (
        pg_db.query(SessionAnalysisCheckpoint)
        .filter(SessionAnalysisCheckpoint.id == checkpoint.id)
        .one()
    )
    assert refreshed.state == "success"
    assert refreshed.event_payload is not None
    assert refreshed.total_tokens == 15
    assert refreshed.prompt_tokens == 10
    assert refreshed.completion_tokens == 5


def test_checkpoint_write_rejects_stale_worker_fencing_mismatch(
    pg_db: Session,
) -> None:
    """When a recovery worker bumps ``TaskLog.detail_json[generation]`` the
    stale worker's claim no longer matches and ``enforce_fencing``
    raises :class:`LateWorkerFencingError`.

    Mirrors the heartbeat recovery path: the lease expired, the
    maintenance task bumped ``recovery_attempt`` and mirrored it into
    ``detail_json[generation]``, a new worker claimed the lease with
    the bumped generation. The previous worker's :class:`ClaimGuards`
    must be rejected so it cannot write.
    """
    _, session_id = _seed_source_with_video_files(
        pg_db, file_count=3, file_duration_seconds=60
    )
    task_log = _bind_running_task_log(pg_db, session_id=session_id, queue_task_id="q-stale")

    stale_guards = _build_guards(task_log)

    sub_chunk = SubChunkPlan(
        chunk_index=0,
        sub_chunk_index=0,
        start_offset_seconds=0,
        duration_seconds=60,
        file_paths=("/tmp/fake/clip-0000.mp4",),
    )
    checkpoint = get_or_create_checkpoint(
        pg_db,
        session_id=session_id,
        claim=stale_guards,
        sub_chunk=sub_chunk,
        system_prompt="sys",
        user_prompt="user",
        video_data_url="data:video/mp4;base64,AAA",
    )
    pg_db.commit()

    # Simulate the recovery take-over: bump generation + lease_owner
    # so the stale claim no longer matches. Wrap in a SAVEPOINT so the
    # rollback after the fencing raise only discards the bump and any
    # in-flight write; setup rows in the outer transaction survive.
    detail = task_log.detail_json if isinstance(task_log.detail_json, dict) else {}
    detail["generation"] = stale_guards.generation + 1
    task_log.detail_json = detail
    task_log.lease_owner = "q-recovery"
    pg_db.commit()

    sp = pg_db.connection().begin_nested()
    try:
        with pytest.raises(LateWorkerFencingError):
            write_sub_chunk_checkpoint(
                pg_db,
                claim=stale_guards,
                checkpoint=checkpoint,
                recognition_result=_recognition_result(0),
                prompt_tokens=10,
                completion_tokens=5,
                total_tokens=15,
            )
    finally:
        sp.rollback()

    refreshed = (
        pg_db.query(SessionAnalysisCheckpoint)
        .filter(SessionAnalysisCheckpoint.id == checkpoint.id)
        .one()
    )
    assert refreshed.state == "pending"
    assert refreshed.event_payload is None


# ---------------------------------------------------------------------------
# checkpoint_failure — late-worker isolation + failure bookkeeping
# ---------------------------------------------------------------------------


def test_late_worker_cannot_write_checkpoint_or_event(
    pg_db: Session,
) -> None:
    """After a recovery take-over the old worker's write is rejected and
    no :class:`EventRecord` lands — the production fencing guarantee.

    Walks the full path: a checkpoint exists in ``pending``, the new
    worker takes the lease (generation bumped), the old worker tries
    to finalise. The fencing rejection means no event row can be
    written either (the events table is owned by the new worker via
    the canonical finalize path).
    """
    _, session_id = _seed_source_with_video_files(
        pg_db, file_count=3, file_duration_seconds=60
    )
    task_log = _bind_running_task_log(pg_db, session_id=session_id, queue_task_id="q-old")
    stale_guards = _build_guards(task_log)

    sub_chunk = SubChunkPlan(
        chunk_index=0,
        sub_chunk_index=0,
        start_offset_seconds=0,
        duration_seconds=60,
        file_paths=("/tmp/fake/clip-0000.mp4",),
    )
    checkpoint = get_or_create_checkpoint(
        pg_db,
        session_id=session_id,
        claim=stale_guards,
        sub_chunk=sub_chunk,
        system_prompt="sys",
        user_prompt="user",
        video_data_url="data:video/mp4;base64,AAA",
    )
    pg_db.commit()

    detail = task_log.detail_json if isinstance(task_log.detail_json, dict) else {}
    detail["generation"] = stale_guards.generation + 1
    task_log.detail_json = detail
    task_log.lease_owner = "q-new"
    pg_db.commit()

    sp = pg_db.connection().begin_nested()
    try:
        with pytest.raises(LateWorkerFencingError):
            write_sub_chunk_checkpoint(
                pg_db,
                claim=stale_guards,
                checkpoint=checkpoint,
                recognition_result=_recognition_result(0),
                prompt_tokens=10,
                completion_tokens=5,
                total_tokens=15,
            )
    finally:
        sp.rollback()

    assert pg_db.query(EventRecord).filter(EventRecord.session_id == session_id).count() == 0
    refreshed = (
        pg_db.query(SessionAnalysisCheckpoint)
        .filter(SessionAnalysisCheckpoint.id == checkpoint.id)
        .one()
    )
    assert refreshed.state == "pending"


def test_failure_marks_checkpoint_failed_and_returns_recovery_signal(
    pg_db: Session,
) -> None:
    """A LLM / parse exception leaves the checkpoint in ``state=error`` and
    finalises the TaskLog as ``FAILED`` so the orchestrator can decide
    between PARTIAL (resume) and FAILED (give up)."""
    _, session_id = _seed_source_with_video_files(
        pg_db, file_count=3, file_duration_seconds=60
    )
    task_log = _bind_running_task_log(pg_db, session_id=session_id, queue_task_id="q-fail")
    guards = _build_guards(task_log)

    sub_chunk = SubChunkPlan(
        chunk_index=0,
        sub_chunk_index=0,
        start_offset_seconds=0,
        duration_seconds=60,
        file_paths=("/tmp/fake/clip-0000.mp4",),
    )
    checkpoint = get_or_create_checkpoint(
        pg_db,
        session_id=session_id,
        claim=guards,
        sub_chunk=sub_chunk,
        system_prompt="sys",
        user_prompt="user",
        video_data_url="data:video/mp4;base64,AAA",
    )
    pg_db.commit()

    exc = RuntimeError("provider failed")
    mark_checkpoint_error(pg_db, claim=guards, checkpoint=checkpoint, error=exc)
    record_recovery_failure(
        pg_db,
        claim=guards,
        task_log=task_log,
        error=exc,
        failed_chunk_index=0,
        failed_sub_chunk_index=0,
        last_prompt_text=None,
        last_response_text=None,
        last_raw_response_text=None,
        priority="hot",
        session_id=session_id,
    )
    finalize_task_log(task_log, TaskStatus.FAILED, "provider failed", {"session_id": session_id})
    pg_db.commit()

    refreshed = (
        pg_db.query(SessionAnalysisCheckpoint)
        .filter(SessionAnalysisCheckpoint.id == checkpoint.id)
        .one()
    )
    assert refreshed.state == "error"
    assert refreshed.last_error == "provider failed"
    assert refreshed.error_type == "RuntimeError"

    refreshed_task_log = pg_db.query(TaskLog).filter(TaskLog.id == task_log.id).one()
    assert refreshed_task_log.status == TaskStatus.FAILED
    assert refreshed_task_log.detail_json["failed_chunk_index"] == 0
    assert refreshed_task_log.detail_json["failed_sub_chunk_index"] == 0
    assert refreshed_task_log.detail_json["error_type"] == "RuntimeError"

    assert (
        pg_db.query(PipelineTransitionLog)
        .filter(
            PipelineTransitionLog.aggregate_id == session_id,
            PipelineTransitionLog.aggregate_type == "VideoSession",
        )
        .count()
        == 0
    )


# ---------------------------------------------------------------------------
# Resumed run: state=success sub-chunks are skipped
# ---------------------------------------------------------------------------


def test_partial_resume_skips_successful_sub_chunks(pg_db: Session) -> None:
    """When a resumed run calls ``get_or_create_checkpoint`` on a sub-chunk
    that the previous run already wrote ``state=success`` the function
    returns the row untouched so the orchestrator can skip the LLM
    call (no re-charge). A previously-failed sub-chunk stays in
    ``state=error`` so the orchestrator's :func:`mark_checkpoint_processing`
    can flip it back to ``processing`` (via ``RESUMABLE_STATES``) and
    retry only the failed sub-chunks."""
    _, session_id = _seed_source_with_video_files(
        pg_db, file_count=3, file_duration_seconds=60
    )
    task_log = _bind_running_task_log(pg_db, session_id=session_id, queue_task_id="q-resume")
    guards = _build_guards(task_log)

    sub_chunks = [
        SubChunkPlan(
            chunk_index=0,
            sub_chunk_index=index,
            start_offset_seconds=index * 60,
            duration_seconds=60,
            file_paths=(f"/tmp/fake/clip-{index:04d}.mp4",),
        )
        for index in range(3)
    ]

    # First pass: sub-chunk 0 succeeds, sub-chunk 1 fails.
    success_checkpoint = get_or_create_checkpoint(
        pg_db,
        session_id=session_id,
        claim=guards,
        sub_chunk=sub_chunks[0],
        system_prompt="sys",
        user_prompt="user",
        video_data_url="data:video/mp4;base64,AAA",
    )
    pg_db.commit()
    write_sub_chunk_checkpoint(
        pg_db,
        claim=guards,
        checkpoint=success_checkpoint,
        recognition_result=_recognition_result(0),
        prompt_tokens=10,
        completion_tokens=5,
        total_tokens=15,
    )
    pg_db.commit()

    failed_checkpoint = get_or_create_checkpoint(
        pg_db,
        session_id=session_id,
        claim=guards,
        sub_chunk=sub_chunks[1],
        system_prompt="sys",
        user_prompt="user",
        video_data_url="data:video/mp4;base64,BBB",
    )
    pg_db.commit()
    mark_checkpoint_error(
        pg_db,
        claim=guards,
        checkpoint=failed_checkpoint,
        error=RuntimeError("provider failed"),
    )
    pg_db.commit()

    # Resumed run: the success row is returned untouched so the caller
    # skips the LLM call. The failed row stays in ``state=error`` — the
    # orchestrator checks ``state != 'success'`` to decide whether to
    # call the LLM. A brand-new sub-chunk comes back in ``pending``.
    resumed_success = get_or_create_checkpoint(
        pg_db,
        session_id=session_id,
        claim=guards,
        sub_chunk=sub_chunks[0],
        system_prompt="sys",
        user_prompt="user",
        video_data_url="data:video/mp4;base64,AAA",
    )
    resumed_failed = get_or_create_checkpoint(
        pg_db,
        session_id=session_id,
        claim=guards,
        sub_chunk=sub_chunks[1],
        system_prompt="sys",
        user_prompt="user",
        video_data_url="data:video/mp4;base64,BBB",
    )
    fresh = get_or_create_checkpoint(
        pg_db,
        session_id=session_id,
        claim=guards,
        sub_chunk=sub_chunks[2],
        system_prompt="sys",
        user_prompt="user",
        video_data_url="data:video/mp4;base64,CCC",
    )

    assert resumed_success.id == success_checkpoint.id
    assert resumed_success.state == "success"
    assert resumed_success.total_tokens == 15

    assert resumed_failed.id == failed_checkpoint.id
    assert resumed_failed.state == "error"
    assert resumed_failed.last_error == "provider failed"

    assert fresh.id != success_checkpoint.id and fresh.id != failed_checkpoint.id
    assert fresh.state == "pending"


# ---------------------------------------------------------------------------
# write_token_usage — checkpoint -> LLMUsageLog wiring
# ---------------------------------------------------------------------------


def test_write_token_usage_persists_llm_usage_log(pg_db: Session, monkeypatch) -> None:
    """The checkpoint writer can persist the LLMUsageLog row bound to the
    checkpoint via the existing ``record_token_usage`` helper."""
    _, session_id = _seed_source_with_video_files(
        pg_db, file_count=3, file_duration_seconds=60
    )
    task_log = _bind_running_task_log(pg_db, session_id=session_id, queue_task_id="q-tok")
    guards = _build_guards(task_log)

    sub_chunk = SubChunkPlan(
        chunk_index=0,
        sub_chunk_index=0,
        start_offset_seconds=0,
        duration_seconds=60,
        file_paths=("/tmp/fake/clip-0000.mp4",),
    )
    checkpoint = get_or_create_checkpoint(
        pg_db,
        session_id=session_id,
        claim=guards,
        sub_chunk=sub_chunk,
        system_prompt="sys",
        user_prompt="user",
        video_data_url="data:video/mp4;base64,AAA",
    )
    pg_db.commit()

    captured: dict[str, object] = {}

    def _fake_record_token_usage(
        db: Session,
        *,
        provider_id: int,
        provider_name_snapshot: str,
        scene: str,
        usage: dict[str, int],
        session_id: int,
        analysis_checkpoint_id: int | None = None,
    ) -> None:
        captured["provider_id"] = provider_id
        captured["provider_name_snapshot"] = provider_name_snapshot
        captured["scene"] = scene
        captured["usage"] = usage
        captured["session_id"] = session_id
        captured["analysis_checkpoint_id"] = analysis_checkpoint_id

    from types import SimpleNamespace

    provider = SimpleNamespace(id=42, provider_name="local")

    write_token_usage(
        pg_db,
        provider=provider,
        checkpoint=checkpoint,
        session_id=session_id,
        usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        record_token_usage_fn=_fake_record_token_usage,
    )

    assert captured["provider_id"] == 42
    assert captured["provider_name_snapshot"] == "local"
    assert captured["scene"] == "video_analysis"
    assert captured["usage"]["total_tokens"] == 15
    assert captured["session_id"] == session_id
    assert captured["analysis_checkpoint_id"] == checkpoint.id
