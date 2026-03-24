from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from src.models.event_record import EventRecord
from src.models.task_log import TaskLog
from src.models.video_session import VideoSession
from src.models.video_source import VideoSource
from src.services.pipeline_constants import TaskStatus
from src.services.session_analysis_video import SessionVideoChunk
from src.services.video_analysis.schemas import (
    RecognitionResultDTO,
    RecognizedEventDTO,
    SessionSummaryDTO,
)
from src.tasks.analyzer import analyze_session_task


def _new_session_factory():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    VideoSource.__table__.create(bind=engine)
    VideoSession.__table__.create(bind=engine)
    EventRecord.__table__.create(bind=engine)
    TaskLog.__table__.create(bind=engine)
    return sessionmaker(bind=engine, autocommit=False, autoflush=False)


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
        ):
            return response_text

        def get_last_usage(self):
            return None

        def get_last_raw_response_text(self):
            return '{"choices":[{"message":{"content":null,"reasoning":"debug"}}]}'

    monkeypatch.setattr("src.tasks.analyzer.SessionLocal", session_factory)
    monkeypatch.setattr(
        "src.tasks.analyzer.build_session_video_chunks",
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
        "src.tasks.analyzer.build_chunk_video_data_url",
        lambda chunk: "data:video/mp4;base64,AAAA",
    )
    monkeypatch.setattr(
        "src.tasks.analyzer._build_provider_client",
        lambda db: (_FakeClient(), SimpleNamespace(id=1, provider_name="mock-provider")),
    )
    monkeypatch.setattr("src.tasks.analyzer.build_home_context", lambda db: {})
    monkeypatch.setattr("src.tasks.analyzer.enforce_token_quota", lambda db, provider: None)
    monkeypatch.setattr(
        "src.tasks.analyzer.record_token_usage",
        lambda db, provider_id, provider_name_snapshot, scene, usage: None,
    )


def test_analyze_session_replaces_old_events(monkeypatch) -> None:
    session_factory = _new_session_factory()
    db = session_factory()
    try:
        source_id, session_id = _seed_source_and_session(db)
        _seed_old_event(db, source_id, session_id, description="old")
        db.commit()
    finally:
        db.close()

    _mock_common(monkeypatch, session_factory)
    monkeypatch.setattr(
        "src.tasks.analyzer.parse_video_recognition_output",
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


def test_analyze_session_empty_result_clears_old_events(monkeypatch) -> None:
    session_factory = _new_session_factory()
    db = session_factory()
    try:
        source_id, session_id = _seed_source_and_session(db)
        _seed_old_event(db, source_id, session_id)
        db.commit()
    finally:
        db.close()

    _mock_common(monkeypatch, session_factory)
    monkeypatch.setattr(
        "src.tasks.analyzer.parse_video_recognition_output",
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


def test_analyze_session_failure_keeps_old_events(monkeypatch) -> None:
    session_factory = _new_session_factory()
    db = session_factory()
    try:
        source_id, session_id = _seed_source_and_session(db)
        _seed_old_event(db, source_id, session_id, description="keep me")
        db.commit()
    finally:
        db.close()

    _mock_common(monkeypatch, session_factory)
    monkeypatch.setattr(
        "src.tasks.analyzer.parse_video_recognition_output",
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
        assert session.analysis_status == "failed"
    finally:
        verify_db.close()


def test_analyze_session_failure_logs_prompt_and_raw_response(monkeypatch, caplog) -> None:
    session_factory = _new_session_factory()
    db = session_factory()
    try:
        source_id, session_id = _seed_source_and_session(db)
        _seed_old_event(db, source_id, session_id, description="keep me")
        db.commit()
    finally:
        db.close()

    _mock_common(monkeypatch, session_factory)
    monkeypatch.setattr("src.tasks.analyzer._build_prompt", lambda *args: "final prompt text")
    monkeypatch.setattr(
        "src.tasks.analyzer.parse_video_recognition_output",
        lambda response_text: (_ for _ in ()).throw(RuntimeError("parse failed")),
    )

    with pytest.raises(RuntimeError):
        analyze_session_task.run(session_id=session_id)

    assert "final prompt text" in caplog.text
    assert '"reasoning":"debug"' in caplog.text


def test_analyze_session_deadlock_retries_with_exponential_backoff(monkeypatch) -> None:
    session_factory = _new_session_factory()
    db = session_factory()
    try:
        source_id, session_id = _seed_source_and_session(db)
        _seed_old_event(db, source_id, session_id, description="keep me")
        db.commit()
    finally:
        db.close()

    _mock_common(monkeypatch, session_factory)
    monkeypatch.setattr(
        "src.tasks.analyzer.parse_video_recognition_output",
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
        "src.tasks.analyzer._replace_session_events",
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


def test_analyze_session_skips_when_already_analyzing(monkeypatch) -> None:
    session_factory = _new_session_factory()
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


def test_analyze_session_honors_cancel_requested(monkeypatch) -> None:
    session_factory = _new_session_factory()
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
