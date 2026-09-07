from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.models.event_record import EventRecord
from src.models.llm_usage_log import LLMUsageLog
from src.models.session_analysis_checkpoint import SessionAnalysisCheckpoint
from src.models.video_session import VideoSession
from src.models.video_source import VideoSource
from src.services.session_analysis_video import SessionVideoChunk, SubChunk
from src.services.video_analysis.schemas import (
    RecognitionResultDTO,
    RecognizedEventDTO,
    SessionSummaryDTO,
)
from src.tasks.analyzer import analyze_session_task


@pytest.mark.postgres
def test_analysis_checkpoint_unique_work_key_on_postgres(postgres_migrated_engine) -> None:
    db = Session(postgres_migrated_engine)
    try:
        source = VideoSource(
            source_name="PG camera",
            camera_name="PG camera",
            location_name="home",
            source_type="local_directory",
            config_json={"root_path": "/tmp"},
            enabled=True,
        )
        db.add(source)
        db.flush()
        session = VideoSession(
            source_id=source.id,
            session_start_time=datetime.now(timezone.utc),
            session_end_time=datetime.now(timezone.utc),
            analysis_status="sealed",
        )
        db.add(session)
        db.flush()
        first = SessionAnalysisCheckpoint(
            session_id=session.id,
            analysis_run_id="run",
            chunk_index=0,
            sub_chunk_index=0,
            start_offset_seconds=0,
            input_fingerprint="fingerprint",
            state="success",
        )
        db.add(first)
        db.flush()
        duplicate = SessionAnalysisCheckpoint(
            session_id=session.id,
            analysis_run_id="run",
            chunk_index=0,
            sub_chunk_index=0,
            start_offset_seconds=0,
            input_fingerprint="fingerprint",
            state="success",
        )
        db.add(duplicate)
        with pytest.raises(IntegrityError):
            db.flush()
    finally:
        db.rollback()
        db.close()


@pytest.mark.postgres
def test_analyze_session_failure_then_retry_resumes_only_remaining_on_postgres(
    postgres_migrated_engine, monkeypatch
) -> None:
    def _recognition_result(index: int) -> RecognitionResultDTO:
        return RecognitionResultDTO(
            session_summary=SessionSummaryDTO(
                summary_text=f"summary-{index}",
                activity_level="medium",
                main_subjects=["PG"],
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

    db = Session(postgres_migrated_engine)
    try:
        source = VideoSource(
            source_name="PG retry camera",
            camera_name="PG retry camera",
            location_name="home",
            source_type="local_directory",
            config_json={"root_path": "/tmp"},
            enabled=True,
        )
        db.add(source)
        db.flush()
        session = VideoSession(
            source_id=source.id,
            session_start_time=datetime.now(timezone.utc),
            session_end_time=datetime.now(timezone.utc),
            total_duration_seconds=180,
            analysis_status="sealed",
        )
        db.add(session)
        db.commit()
        session_id = session.id
    finally:
        db.close()

    @contextmanager
    def _task_session():
        task_db = Session(postgres_migrated_engine)
        try:
            yield task_db
        finally:
            task_db.close()

    remaining = ["0", "1", "raise"]
    first_calls: list[str] = []

    class _FakeClient:
        def chat_completion(self, **kwargs):
            del kwargs
            response = remaining.pop(0)
            first_calls.append(response)
            if response == "raise":
                raise RuntimeError("provider failed")
            return response

        def get_last_usage(self):
            return {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}

        def get_last_raw_response_text(self):
            return "raw"

    monkeypatch.setattr("src.tasks._analyzer_orchestration.task_db_session", _task_session)
    monkeypatch.setattr(
        "src.tasks._analyzer_orchestration.build_session_video_chunks",
        lambda db, session_id, chunk_seconds: [SessionVideoChunk(0, 0, 180, ["/tmp/pg.mp4"])],
    )
    monkeypatch.setattr(
        "src.tasks._analyzer_orchestration.build_chunk_sub_chunks",
        lambda chunk, db, sub_chunk_seconds: [
            SubChunk(0, index, index * 60, 60, [f"/tmp/pg-{index}.mp4"]) for index in range(3)
        ],
    )
    monkeypatch.setattr(
        "src.tasks._analyzer_orchestration.build_chunk_video_data_url",
        lambda chunk: f"data:video/mp4;base64,{chunk.start_offset_seconds}",
    )
    monkeypatch.setattr("src.tasks._analyzer_orchestration.build_home_context", lambda db: {})
    monkeypatch.setattr(
        "src.tasks._analyzer_orchestration.enforce_token_quota",
        lambda db, provider: None,
    )
    monkeypatch.setattr(
        "src.tasks._analyzer_orchestration._build_provider_client",
        lambda db: (_FakeClient(), SimpleNamespace(id=None, provider_name="PG fake")),
    )
    monkeypatch.setattr(
        "src.tasks._analyzer_orchestration.parse_video_recognition_output",
        lambda response: _recognition_result(int(response)),
    )

    with pytest.raises(RuntimeError, match="provider failed"):
        analyze_session_task.run(session_id=session_id)

    verify = Session(postgres_migrated_engine)
    try:
        checkpoints = (
            verify.query(SessionAnalysisCheckpoint)
            .order_by(SessionAnalysisCheckpoint.sub_chunk_index)
            .all()
        )
        failed_session = verify.query(VideoSession).filter(VideoSession.id == session_id).one()
        assert first_calls == ["0", "1", "raise"]
        assert [checkpoint.state for checkpoint in checkpoints] == ["success", "success", "error"]
        assert sum(checkpoint.total_tokens for checkpoint in checkpoints) == 30
        assert failed_session.analysis_status == "partial"
        assert verify.query(EventRecord).filter(EventRecord.session_id == session_id).count() == 0
        assert verify.query(LLMUsageLog).filter(LLMUsageLog.session_id == session_id).count() == 2
    finally:
        verify.close()

    retry_responses = ["2"]
    retry_calls: list[str] = []

    class _RetryClient(_FakeClient):
        def chat_completion(self, **kwargs):
            del kwargs
            response = retry_responses.pop(0)
            retry_calls.append(response)
            return response

    monkeypatch.setattr(
        "src.tasks._analyzer_orchestration._build_provider_client",
        lambda db: (_RetryClient(), SimpleNamespace(id=None, provider_name="PG fake")),
    )
    result = analyze_session_task.run(session_id=session_id)

    verify = Session(postgres_migrated_engine)
    try:
        completed_session = verify.query(VideoSession).filter(VideoSession.id == session_id).one()
        events = verify.query(EventRecord).filter(EventRecord.session_id == session_id).all()
        usage = verify.query(LLMUsageLog).filter(LLMUsageLog.session_id == session_id).all()
        assert retry_calls == ["2"]
        assert result["events_created"] == 3
        assert completed_session.analysis_status == "success"
        assert len(events) == 3
        assert {event.summary for event in events} == {"event-0", "event-1", "event-2"}
        assert len(usage) == 3
        assert sum(item.total_tokens for item in usage) == 45
    finally:
        verify.close()
