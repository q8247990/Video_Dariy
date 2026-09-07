import logging
from collections.abc import Callable
from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from src.models.event_record import EventRecord
from src.models.session_analysis_checkpoint import SessionAnalysisCheckpoint
from src.models.task_log import TaskLog
from src.models.video_session import VideoSession
from src.models.video_source import VideoSource
from src.services.analysis.checkpoint_writer import _checkpoint_for_work
from src.services.pipeline_constants import TaskStatus
from src.services.provider_key_crypto import ProviderKeyDecryptionError
from src.services.session_analysis_video import SessionVideoChunk, SubChunk
from src.services.video_analysis.schemas import (
    RecognitionResultDTO,
    RecognizedEventDTO,
    SessionSummaryDTO,
)
from src.tasks.analyzer import analyze_session_task

SessionFactory = Callable[[], Session]


def _seed_source_and_session(db: Session) -> tuple[int, int]:
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

    session = VideoSession(
        source_id=source.id,
        session_start_time=datetime(2026, 3, 15, 10, 0, 0),
        session_end_time=datetime(2026, 3, 15, 10, 5, 0),
        total_duration_seconds=300,
        analysis_status="sealed",
    )
    db.add(session)
    db.flush()
    return source.id, session.id


def _seed_old_event(
    db: Session, source_id: int, session_id: int, description: str = "old event"
) -> int:
    event = EventRecord(
        source_id=source_id,
        session_id=session_id,
        event_start_time=datetime(2026, 3, 15, 10, 0, 10),
        event_end_time=datetime(2026, 3, 15, 10, 0, 20),
        description=description,
        event_type="member_appear",
        action_type="member_appear",
        title=description,
        summary=description,
        confidence_score=0.8,
        offset_start_sec=10,
        offset_end_sec=20,
    )
    db.add(event)
    db.flush()
    return event.id


def _mock_common(monkeypatch, session_factory, response_text: str = "{}") -> None:
    class _FakeClient:
        def chat_completion(
            self,
            messages,
            temperature=0,
            max_tokens=None,
            response_format=None,
            extra_body=None,
        ):
            del messages, temperature, max_tokens, response_format, extra_body
            return response_text

        def get_last_usage(self):
            return None

        def get_last_raw_response_text(self):
            return '{"choices":[{"message":{"content":null,"reasoning":"debug"}}]}'

    from contextlib import contextmanager

    @contextmanager
    def _fake_task_db_session():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    monkeypatch.setattr("src.tasks._analyzer_orchestration.task_db_session", _fake_task_db_session)
    monkeypatch.setattr(
        "src.tasks._analyzer_orchestration.build_session_video_chunks",
        lambda db, session_id, chunk_seconds: [
            SessionVideoChunk(
                chunk_index=0,
                start_offset_seconds=0,
                duration_seconds=60,
                file_paths=["/tmp/mock.mp4"],
            )
        ],
    )
    monkeypatch.setattr(
        "src.tasks._analyzer_orchestration.build_chunk_video_data_url",
        lambda chunk: "data:video/mp4;base64,AAAA",
    )
    monkeypatch.setattr(
        "src.tasks._analyzer_orchestration._build_provider_client",
        lambda db: (_FakeClient(), SimpleNamespace(id=1, provider_name="mock-provider")),
    )
    monkeypatch.setattr("src.tasks._analyzer_orchestration.build_home_context", lambda db: {})
    monkeypatch.setattr(
        "src.tasks._analyzer_orchestration.enforce_token_quota",
        lambda db, provider: None,
    )
    monkeypatch.setattr(
        "src.tasks._analyzer_orchestration.record_token_usage",
        lambda db, provider_id, provider_name_snapshot, scene, usage, **kwargs: None,
    )


def _recognition_result(index: int) -> RecognitionResultDTO:
    return RecognitionResultDTO(
        session_summary=SessionSummaryDTO(
            summary_text=f"summary-{index}",
            activity_level="medium",
            main_subjects=["爸爸"],
            has_important_event=True,
        ),
        events=[
            RecognizedEventDTO(
                offset_start_sec=1,
                offset_end_sec=5,
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


def _mock_three_sub_chunks(monkeypatch, session_factory, responses: list[str]) -> list[int]:
    calls: list[int] = []

    class _FakeClient:
        def chat_completion(self, **kwargs):
            del kwargs
            calls.append(len(calls))
            response = responses.pop(0)
            if response == "raise":
                raise RuntimeError("provider failed")
            return response

        def get_last_usage(self):
            return {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}

        def get_last_raw_response_text(self):
            return "raw"

    _mock_common(monkeypatch, session_factory)
    monkeypatch.setattr(
        "src.tasks._analyzer_orchestration.build_chunk_sub_chunks",
        lambda chunk, db, sub_chunk_seconds: [
            SubChunk(
                chunk_index=0,
                sub_chunk_index=index,
                start_offset_seconds=index * 60,
                duration_seconds=60,
                file_paths=[f"/tmp/mock-{index}.mp4"],
            )
            for index in range(3)
        ],
    )
    monkeypatch.setattr(
        "src.tasks._analyzer_orchestration.session_chunk_from_sub_chunk",
        lambda sub_chunk, parent_chunk_index: sub_chunk,
    )
    monkeypatch.setattr(
        "src.tasks._analyzer_orchestration._build_provider_client",
        lambda db: (_FakeClient(), SimpleNamespace(id=1, provider_name="mock-provider")),
    )
    monkeypatch.setattr(
        "src.tasks._analyzer_orchestration.parse_video_recognition_output",
        lambda response: _recognition_result(int(response)),
    )
    return calls


def test_analyze_session_partial_failure_preserves_completed_sub_chunks(
    monkeypatch, pg_db_factory: SessionFactory
) -> None:
    session_factory = pg_db_factory
    db = session_factory()
    try:
        _, session_id = _seed_source_and_session(db)
        db.commit()
    finally:
        db.close()

    _mock_three_sub_chunks(monkeypatch, session_factory, ["0", "1", "raise"])

    with pytest.raises(RuntimeError, match="provider failed"):
        analyze_session_task.run(session_id=session_id)

    verify_db = session_factory()
    try:
        checkpoints = (
            verify_db.query(SessionAnalysisCheckpoint)
            .order_by(SessionAnalysisCheckpoint.sub_chunk_index)
            .all()
        )
        session = verify_db.query(VideoSession).filter(VideoSession.id == session_id).one()
        assert [checkpoint.state for checkpoint in checkpoints] == ["success", "success", "error"]
        assert checkpoints[0].event_payload is not None
        assert checkpoints[1].total_tokens == 15
        assert checkpoints[2].last_error == "provider failed"
        assert checkpoints[2].error_type == "RuntimeError"
        assert session.analysis_status == "partial"
        assert (
            verify_db.query(EventRecord).filter(EventRecord.session_id == session_id).count() == 0
        )
    finally:
        verify_db.close()


def test_analyze_session_retry_resumes_from_first_non_success_without_recharging(
    monkeypatch,
    pg_db_factory: SessionFactory,
) -> None:
    session_factory = pg_db_factory
    db = session_factory()
    try:
        _, session_id = _seed_source_and_session(db)
        db.commit()
    finally:
        db.close()

    first_calls = _mock_three_sub_chunks(monkeypatch, session_factory, ["0", "1", "raise"])
    with pytest.raises(RuntimeError):
        analyze_session_task.run(session_id=session_id)
    retry_calls = _mock_three_sub_chunks(monkeypatch, session_factory, ["2"])

    result = analyze_session_task.run(session_id=session_id)

    verify_db = session_factory()
    try:
        session = verify_db.query(VideoSession).filter(VideoSession.id == session_id).one()
        checkpoints = verify_db.query(SessionAnalysisCheckpoint).all()
        events = verify_db.query(EventRecord).filter(EventRecord.session_id == session_id).all()
        assert first_calls == [0, 1, 2]
        assert retry_calls == [0]
        assert result["events_created"] == 3
        assert session.analysis_status == "success"
        assert len(checkpoints) == 3
        assert sum(checkpoint.total_tokens for checkpoint in checkpoints) == 45
        assert len(events) == 3
        assert {event.summary for event in events} == {"event-0", "event-1", "event-2"}
    finally:
        verify_db.close()


def test_analyze_session_duplicate_retry_does_not_duplicate_events_or_usage(
    monkeypatch, pg_db_factory: SessionFactory
) -> None:
    session_factory = pg_db_factory
    db = session_factory()
    try:
        _, session_id = _seed_source_and_session(db)
        db.commit()
    finally:
        db.close()

    calls = _mock_three_sub_chunks(monkeypatch, session_factory, ["0", "1", "2"])
    analyze_session_task.run(session_id=session_id)
    retry_result = analyze_session_task.run(session_id=session_id)

    verify_db = session_factory()
    try:
        assert calls == [0, 1, 2]
        assert retry_result["skipped"] is True
        assert (
            verify_db.query(EventRecord).filter(EventRecord.session_id == session_id).count() == 3
        )
        assert verify_db.query(SessionAnalysisCheckpoint).count() == 3
    finally:
        verify_db.close()


def test_analyze_session_changed_input_invalidates_stale_checkpoints(
    pg_db_factory: SessionFactory,
) -> None:
    session_factory = pg_db_factory
    db = session_factory()
    try:
        _, session_id = _seed_source_and_session(db)
        checkpoint = SessionAnalysisCheckpoint(
            session_id=session_id,
            analysis_run_id="run",
            chunk_index=0,
            sub_chunk_index=0,
            start_offset_seconds=0,
            input_fingerprint="old",
            state="success",
            event_payload={"events": []},
            total_tokens=15,
        )
        db.add(checkpoint)
        db.commit()
        sub_chunk = SubChunk(0, 0, 0, 60, ["/tmp/mock.mp4"])
        updated = _checkpoint_for_work(
            db,
            session_id=session_id,
            analysis_run_id="run",
            chunk_index=0,
            sub_chunk=sub_chunk,
            input_fingerprint="new",
        )
        assert updated.state == "pending"
        assert updated.event_payload is None
        assert updated.total_tokens == 0
    finally:
        db.close()


def test_analyze_session_cancelled_mid_run_partial_not_served(
    monkeypatch, pg_db_factory: SessionFactory
) -> None:
    session_factory = pg_db_factory
    db = session_factory()
    try:
        _, session_id = _seed_source_and_session(db)
        db.commit()
    finally:
        db.close()

    _mock_three_sub_chunks(monkeypatch, session_factory, ["0", "1", "2"])
    calls = {"count": 0}

    def _cancel_after_first(*args, **kwargs):
        del args, kwargs
        calls["count"] += 1
        if calls["count"] > 4:
            from src.services.task_dispatch_control import TaskCancellationRequested

            raise TaskCancellationRequested("cancelled")

    monkeypatch.setattr(
        "src.tasks._analyzer_orchestration.ensure_task_not_cancelled", _cancel_after_first
    )
    result = analyze_session_task.run(session_id=session_id)

    verify_db = session_factory()
    try:
        assert result["cancelled"] is True
        assert (
            verify_db.query(EventRecord).filter(EventRecord.session_id == session_id).count() == 0
        )
        assert verify_db.query(SessionAnalysisCheckpoint).count() == 2
    finally:
        verify_db.close()


def test_analyze_session_replaces_old_events(monkeypatch, pg_db_factory: SessionFactory) -> None:
    session_factory = pg_db_factory
    db = session_factory()
    try:
        source_id, session_id = _seed_source_and_session(db)
        _seed_old_event(db, source_id, session_id, description="old")
        db.commit()
    finally:
        db.close()

    _mock_common(monkeypatch, session_factory)
    monkeypatch.setattr(
        "src.tasks._analyzer_orchestration.parse_video_recognition_output",
        lambda response_text: RecognitionResultDTO(
            session_summary=SessionSummaryDTO(
                summary_text="new summary",
                activity_level="medium",
                main_subjects=["爸爸"],
                has_important_event=True,
            ),
            events=[
                RecognizedEventDTO(
                    offset_start_sec=1,
                    offset_end_sec=5,
                    event_type="member_appear",
                    title="新事件",
                    summary="新事件描述",
                    detail="新事件详细描述",
                    related_entities=[],
                    observed_actions=[],
                    interpreted_state=[],
                    confidence=0.9,
                    importance_level="medium",
                )
            ],
            analysis_notes=[],
        ),
    )

    result = analyze_session_task.run(session_id=session_id)
    assert result["events_created"] == 1

    verify_db = session_factory()
    try:
        events = verify_db.query(EventRecord).filter(EventRecord.session_id == session_id).all()
        assert len(events) == 1
        assert events[0].summary == "新事件描述"
        assert events[0].detail == "新事件详细描述"
    finally:
        verify_db.close()


def test_analyze_session_empty_result_clears_old_events(
    monkeypatch, pg_db_factory: SessionFactory
) -> None:
    session_factory = pg_db_factory
    db = session_factory()
    try:
        source_id, session_id = _seed_source_and_session(db)
        _seed_old_event(db, source_id, session_id)
        db.commit()
    finally:
        db.close()

    _mock_common(monkeypatch, session_factory)
    monkeypatch.setattr(
        "src.tasks._analyzer_orchestration.parse_video_recognition_output",
        lambda response_text: RecognitionResultDTO(
            session_summary=SessionSummaryDTO(
                summary_text="no event",
                activity_level="low",
                main_subjects=[],
                has_important_event=False,
            ),
            events=[],
            analysis_notes=[],
        ),
    )

    result = analyze_session_task.run(session_id=session_id)
    assert result["events_created"] == 0

    verify_db = session_factory()
    try:
        count = verify_db.query(EventRecord).filter(EventRecord.session_id == session_id).count()
        assert count == 0
        session = verify_db.query(VideoSession).filter(VideoSession.id == session_id).first()
        assert session is not None
        assert session.analysis_status == "success"
    finally:
        verify_db.close()


def test_analyze_session_failure_keeps_old_events(
    monkeypatch, pg_db_factory: SessionFactory
) -> None:
    session_factory = pg_db_factory
    db = session_factory()
    try:
        source_id, session_id = _seed_source_and_session(db)
        _seed_old_event(db, source_id, session_id, description="keep me")
        db.commit()
    finally:
        db.close()

    _mock_common(monkeypatch, session_factory)
    monkeypatch.setattr(
        "src.tasks._analyzer_orchestration.parse_video_recognition_output",
        lambda response_text: (_ for _ in ()).throw(RuntimeError("parse failed")),
    )

    with pytest.raises(RuntimeError):
        analyze_session_task.run(session_id=session_id)

    verify_db = session_factory()
    try:
        events = verify_db.query(EventRecord).filter(EventRecord.session_id == session_id).all()
        assert len(events) == 1
        assert events[0].summary == "keep me"
        session = verify_db.query(VideoSession).filter(VideoSession.id == session_id).first()
        assert session is not None
        assert session.analysis_status == "partial"
    finally:
        verify_db.close()


def test_analyze_session_failure_logs_prompt_and_raw_response(
    monkeypatch, caplog, pg_db_factory: SessionFactory
) -> None:
    caplog.set_level(logging.ERROR, logger="src.tasks._analyzer_orchestration")
    session_factory = pg_db_factory
    db = session_factory()
    try:
        source_id, session_id = _seed_source_and_session(db)
        _seed_old_event(db, source_id, session_id, description="keep me")
        db.commit()
    finally:
        db.close()

    _mock_common(monkeypatch, session_factory)
    monkeypatch.setattr(
        "src.tasks._analyzer_orchestration._build_prompts",
        lambda *_: ("system", "final prompt text"),
    )
    monkeypatch.setattr(
        "src.tasks._analyzer_orchestration.parse_video_recognition_output",
        lambda response_text: (_ for _ in ()).throw(RuntimeError("parse failed")),
    )

    with pytest.raises(RuntimeError):
        analyze_session_task.run(session_id=session_id)

    assert "final prompt text" in caplog.text
    assert '"reasoning":"debug"' in caplog.text


def test_analyze_session_deadlock_retries_with_exponential_backoff(
    monkeypatch, pg_db_factory: SessionFactory
) -> None:
    session_factory = pg_db_factory
    db = session_factory()
    try:
        source_id, session_id = _seed_source_and_session(db)
        _seed_old_event(db, source_id, session_id, description="keep me")
        db.commit()
    finally:
        db.close()

    _mock_common(monkeypatch, session_factory)
    monkeypatch.setattr(
        "src.tasks._analyzer_orchestration.parse_video_recognition_output",
        lambda response_text: RecognitionResultDTO(
            session_summary=SessionSummaryDTO(
                summary_text="deadlock summary",
                activity_level="medium",
                main_subjects=["爸爸"],
                has_important_event=True,
            ),
            events=[
                RecognizedEventDTO(
                    offset_start_sec=1,
                    offset_end_sec=5,
                    event_type="member_appear",
                    title="新事件",
                    summary="新事件描述",
                    detail="新事件详细描述",
                    related_entities=[],
                    observed_actions=[],
                    interpreted_state=[],
                    confidence=0.9,
                    importance_level="medium",
                )
            ],
            analysis_notes=[],
        ),
    )

    class _DeadlockOrig(Exception):
        def __init__(self) -> None:
            self.pgcode = "40P01"

        def __str__(self) -> str:
            return "deadlock detected"

    monkeypatch.setattr(
        "src.tasks._analyzer_orchestration._replace_session_events",
        lambda db, session_id, events: (_ for _ in ()).throw(
            OperationalError("INSERT INTO event_record ...", {}, _DeadlockOrig())
        ),
    )

    retry_calls: list[int] = []

    def _fake_retry(*, exc, countdown):
        retry_calls.append(countdown)
        raise RuntimeError("retry-triggered")

    monkeypatch.setattr(analyze_session_task, "retry", _fake_retry, raising=False)

    analyze_session_task.push_request(id="retry-task-id", retries=0)
    try:
        with pytest.raises(RuntimeError, match="retry-triggered"):
            analyze_session_task.run(session_id=session_id)
    finally:
        analyze_session_task.pop_request()

    verify_db = session_factory()
    try:
        task_log = verify_db.query(TaskLog).filter(TaskLog.task_target_id == session_id).one()
        session = verify_db.query(VideoSession).filter(VideoSession.id == session_id).one()
        events = verify_db.query(EventRecord).filter(EventRecord.session_id == session_id).all()

        assert retry_calls == [1]
        assert task_log.retry_count == 1
        assert "Deadlock detected, retry 1/3 in 1s" in (task_log.message or "")
        assert session.analysis_status == "sealed"
        assert len(events) == 1
        assert events[0].summary == "keep me"
    finally:
        verify_db.close()


def test_analyze_session_skips_when_already_analyzing(
    monkeypatch, pg_db_factory: SessionFactory
) -> None:
    session_factory = pg_db_factory
    db = session_factory()
    try:
        source_id, session_id = _seed_source_and_session(db)
        _seed_old_event(db, source_id, session_id, description="keep me")
        session = db.query(VideoSession).filter(VideoSession.id == session_id).one()
        session.analysis_status = "analyzing"
        db.commit()
    finally:
        db.close()

    _mock_common(monkeypatch, session_factory)

    result = analyze_session_task.run(session_id=session_id)

    assert result["skipped"] is True
    assert result["reason"] == "already_analyzing"

    verify_db = session_factory()
    try:
        task_log = verify_db.query(TaskLog).filter(TaskLog.task_target_id == session_id).one()
        session = verify_db.query(VideoSession).filter(VideoSession.id == session_id).one()
        events = verify_db.query(EventRecord).filter(EventRecord.session_id == session_id).all()

        assert task_log.status == TaskStatus.SKIPPED
        assert task_log.message == f"Skipped session {session_id}, already analyzing"
        assert session.analysis_status == "analyzing"
        assert len(events) == 1
        assert events[0].summary == "keep me"
    finally:
        verify_db.close()


def test_analyze_session_honors_cancel_requested(
    monkeypatch, pg_db_factory: SessionFactory
) -> None:
    session_factory = pg_db_factory
    db = session_factory()
    try:
        source_id, session_id = _seed_source_and_session(db)
        _seed_old_event(db, source_id, session_id, description="keep me")
        task_log = TaskLog(
            task_type="session_analysis",
            task_target_id=session_id,
            queue_task_id="cancel-task-id",
            status="running",
            cancel_requested=True,
            detail_json={"priority": "hot", "dedupe_key": f"session_analysis|{session_id}"},
        )
        db.add(task_log)
        db.commit()
    finally:
        db.close()

    _mock_common(monkeypatch, session_factory)

    analyze_session_task.push_request(id="cancel-task-id", retries=0)
    try:
        result = analyze_session_task.run(session_id=session_id)
    finally:
        analyze_session_task.pop_request()

    assert result["cancelled"] is True

    verify_db = session_factory()
    try:
        task_log = verify_db.query(TaskLog).filter(TaskLog.task_target_id == session_id).one()
        session = verify_db.query(VideoSession).filter(VideoSession.id == session_id).one()
        events = verify_db.query(EventRecord).filter(EventRecord.session_id == session_id).all()

        assert task_log.status == TaskStatus.CANCELLED
        assert task_log.cancel_requested is True
        assert session.analysis_status == "sealed"
        assert len(events) == 1
        assert events[0].summary == "keep me"
    finally:
        verify_db.close()


def test_provider_build_failure_finalizes_task_log_failed(
    monkeypatch, pg_db_factory: SessionFactory
) -> None:
    """A failure during provider-client build (e.g. API-key decryption) must
    finalize the TaskLog to FAILED, not strand it as a zombie ``running`` row
    with no ``finished_at``/``message``."""
    session_factory = pg_db_factory
    db = session_factory()
    try:
        _, session_id = _seed_source_and_session(db)
        db.commit()
    finally:
        db.close()

    _mock_common(monkeypatch, session_factory)
    monkeypatch.setattr(
        "src.tasks._analyzer_orchestration._build_provider_client",
        lambda db: (_ for _ in ()).throw(ProviderKeyDecryptionError("cannot decrypt")),
    )
    monkeypatch.setattr(
        "src.tasks._analyzer_orchestration.build_chunk_sub_chunks",
        lambda chunk, db, sub_chunk_seconds: [
            SubChunk(
                chunk_index=0,
                sub_chunk_index=0,
                start_offset_seconds=0,
                duration_seconds=60,
                file_paths=["/tmp/mock.mp4"],
            )
        ],
    )

    with pytest.raises(ProviderKeyDecryptionError):
        analyze_session_task.run(session_id=session_id, priority="hot")

    verify_db = session_factory()
    try:
        task_log = (
            verify_db.query(TaskLog)
            .filter(
                TaskLog.task_target_id == session_id,
                TaskLog.task_type == "session_analysis",
            )
            .one()
        )
        session = verify_db.query(VideoSession).filter(VideoSession.id == session_id).one()
        assert task_log.status == TaskStatus.FAILED
        assert task_log.finished_at is not None
        assert task_log.message is not None
        assert session.analysis_status == "failed"
    finally:
        verify_db.close()
