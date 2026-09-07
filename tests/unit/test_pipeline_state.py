from datetime import datetime, timezone

import pytest
from sqlalchemy.orm import Session

from src.models.task_log import TaskLog
from src.models.video_session import VideoSession
from src.models.video_source import VideoSource
from src.services.analysis.claim import claim_session_for_analysis
from src.services.pipeline_constants import SessionAnalysisStatus, TaskStatus, TaskType
from src.services.pipeline_state import (
    SESSION_ALLOWED_TRANSITIONS,
    TASK_LOG_ALLOWED_TRANSITIONS,
    PipelineTransitionConflict,
    transition_session,
    transition_task_log,
)


def _seed_session(pg_db: Session, status: SessionAnalysisStatus) -> VideoSession:
    source = VideoSource(
        source_name="test",
        camera_name="test",
        location_name="test",
        source_type="local_directory",
        config_json={"root_path": "/tmp"},
        enabled=True,
    )
    pg_db.add(source)
    pg_db.flush()
    session = VideoSession(
        source_id=source.id,
        session_start_time=datetime.now(timezone.utc),
        session_end_time=datetime.now(timezone.utc),
        analysis_status=status,
    )
    pg_db.add(session)
    pg_db.commit()
    return session


@pytest.mark.parametrize(
    ("from_status", "to_status"),
    sorted(SESSION_ALLOWED_TRANSITIONS, key=lambda edge: (edge[0].value, edge[1].value)),
)
def test_transition_session_applies_every_legal_edge(
    from_status: SessionAnalysisStatus,
    to_status: SessionAnalysisStatus,
    pg_db: Session,
) -> None:
    session = _seed_session(pg_db, from_status)

    result = transition_session(
        pg_db, session.id, from_status, to_status, reason="test", source="test"
    )
    pg_db.commit()

    pg_db.refresh(session)
    assert result.applied is True
    assert session.analysis_status == to_status


@pytest.mark.parametrize(
    ("from_status", "to_status"),
    sorted(TASK_LOG_ALLOWED_TRANSITIONS, key=lambda edge: (edge[0].value, edge[1].value)),
)
def test_transition_task_log_applies_every_legal_edge(
    from_status: TaskStatus, to_status: TaskStatus, pg_db: Session
) -> None:
    task_log = TaskLog(task_type=TaskType.SESSION_ANALYSIS, status=from_status)
    pg_db.add(task_log)
    pg_db.commit()

    result = transition_task_log(
        pg_db, task_log.id, from_status, to_status, reason="test", source="test"
    )
    pg_db.commit()

    pg_db.refresh(task_log)
    assert result.applied is True
    assert task_log.status == to_status
    assert task_log.detail_json["transition"]["source"] == "test"


def test_transition_session_rejects_open_to_analyzing(pg_db: Session) -> None:
    session = _seed_session(pg_db, SessionAnalysisStatus.OPEN)

    with pytest.raises(PipelineTransitionConflict):
        transition_session(
            pg_db,
            session.id,
            SessionAnalysisStatus.OPEN,
            SessionAnalysisStatus.ANALYZING,
            reason="test",
            source="test",
        )


def test_atomic_claim_allows_exactly_one_owner(pg_db: Session) -> None:
    session = _seed_session(pg_db, SessionAnalysisStatus.SEALED)

    claimed, first_outcome = claim_session_for_analysis(
        pg_db,
        session_id=session.id,
        priority="normal",
        queue_task_id="queue-claim-1",
        analysis_run_id="run-1",
    )
    pg_db.refresh(session)
    duplicate, second_outcome = claim_session_for_analysis(
        pg_db,
        session_id=session.id,
        priority="normal",
        queue_task_id="queue-claim-1",
        analysis_run_id="run-1",
    )

    assert claimed is not None
    assert first_outcome is None
    assert session.analysis_status == SessionAnalysisStatus.ANALYZING
    assert duplicate is None
    assert second_outcome is not None
    assert second_outcome.reason == "already_analyzing"


def test_stale_retry_and_completion_after_cancellation_are_noops(pg_db: Session) -> None:
    session = _seed_session(pg_db, SessionAnalysisStatus.SEALED)
    task_log = TaskLog(
        task_type=TaskType.SESSION_ANALYSIS,
        status=TaskStatus.CANCELLED,
        cancel_requested=True,
    )
    pg_db.add(task_log)
    pg_db.commit()

    stale_retry = transition_session(
        pg_db,
        session.id,
        SessionAnalysisStatus.FAILED,
        SessionAnalysisStatus.SEALED,
        reason="retry",
        source="test",
    )
    cancelled_completion = transition_session(
        pg_db,
        session.id,
        SessionAnalysisStatus.ANALYZING,
        SessionAnalysisStatus.SUCCESS,
        reason="completion",
        source="test",
        task_log=task_log,
    )

    assert stale_retry.applied is False
    assert cancelled_completion.applied is False
