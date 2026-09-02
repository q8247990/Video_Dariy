from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from src.models.pipeline_transition_log import PipelineTransitionLog
from src.models.task_log import TaskLog
from src.models.video_session import VideoSession
from src.models.video_source import VideoSource
from src.services.pipeline_constants import SessionAnalysisStatus, TaskStatus, TaskType
from src.services.pipeline_state import (
    SESSION_ALLOWED_TRANSITIONS,
    TASK_LOG_ALLOWED_TRANSITIONS,
    PipelineTransitionConflict,
    transition_session,
    transition_task_log,
)
from src.tasks.analyzer import _claim_session_for_analysis


def _new_db_session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    VideoSource.__table__.create(bind=engine)
    VideoSession.__table__.create(bind=engine)
    TaskLog.__table__.create(bind=engine)
    PipelineTransitionLog.__table__.create(bind=engine)
    return sessionmaker(bind=engine, autocommit=False, autoflush=False)()


def _seed_session(db: Session, status: SessionAnalysisStatus) -> VideoSession:
    source = VideoSource(
        source_name="test",
        camera_name="test",
        location_name="test",
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
        analysis_status=status,
    )
    db.add(session)
    db.commit()
    return session


@pytest.mark.parametrize(
    ("from_status", "to_status"),
    sorted(SESSION_ALLOWED_TRANSITIONS, key=lambda edge: (edge[0].value, edge[1].value)),
)
def test_transition_session_applies_every_legal_edge(
    from_status: SessionAnalysisStatus, to_status: SessionAnalysisStatus
) -> None:
    db = _new_db_session()
    try:
        session = _seed_session(db, from_status)

        result = transition_session(
            db, session.id, from_status, to_status, reason="test", source="test"
        )
        db.commit()

        db.refresh(session)
        assert result.applied is True
        assert session.analysis_status == to_status
    finally:
        db.close()


@pytest.mark.parametrize(
    ("from_status", "to_status"),
    sorted(TASK_LOG_ALLOWED_TRANSITIONS, key=lambda edge: (edge[0].value, edge[1].value)),
)
def test_transition_task_log_applies_every_legal_edge(
    from_status: TaskStatus, to_status: TaskStatus
) -> None:
    db = _new_db_session()
    try:
        task_log = TaskLog(task_type=TaskType.SESSION_ANALYSIS, status=from_status)
        db.add(task_log)
        db.commit()

        result = transition_task_log(
            db, task_log.id, from_status, to_status, reason="test", source="test"
        )
        db.commit()

        db.refresh(task_log)
        assert result.applied is True
        assert task_log.status == to_status
        assert task_log.detail_json["transition"]["source"] == "test"
    finally:
        db.close()


def test_transition_session_rejects_open_to_analyzing() -> None:
    db = _new_db_session()
    try:
        session = _seed_session(db, SessionAnalysisStatus.OPEN)

        with pytest.raises(PipelineTransitionConflict):
            transition_session(
                db,
                session.id,
                SessionAnalysisStatus.OPEN,
                SessionAnalysisStatus.ANALYZING,
                reason="test",
                source="test",
            )
    finally:
        db.close()


def test_atomic_claim_allows_exactly_one_owner() -> None:
    db = _new_db_session()
    try:
        session = _seed_session(db, SessionAnalysisStatus.SEALED)

        claimed, first_reason = _claim_session_for_analysis(db, session.id)
        duplicate, second_reason = _claim_session_for_analysis(db, session.id)

        assert claimed is not None
        assert first_reason is None
        assert duplicate is None
        assert second_reason == "already_analyzing"
    finally:
        db.close()


def test_stale_retry_and_completion_after_cancellation_are_noops() -> None:
    db = _new_db_session()
    try:
        session = _seed_session(db, SessionAnalysisStatus.SEALED)
        task_log = TaskLog(
            task_type=TaskType.SESSION_ANALYSIS,
            status=TaskStatus.CANCELLED,
            cancel_requested=True,
        )
        db.add(task_log)
        db.commit()

        stale_retry = transition_session(
            db,
            session.id,
            SessionAnalysisStatus.FAILED,
            SessionAnalysisStatus.SEALED,
            reason="retry",
            source="test",
        )
        cancelled_completion = transition_session(
            db,
            session.id,
            SessionAnalysisStatus.ANALYZING,
            SessionAnalysisStatus.SUCCESS,
            reason="completion",
            source="test",
            task_log=task_log,
        )

        assert stale_retry.applied is False
        assert cancelled_completion.applied is False
    finally:
        db.close()
