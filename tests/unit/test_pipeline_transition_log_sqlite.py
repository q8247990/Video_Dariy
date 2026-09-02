"""SQLite unit tests for the append-only pipeline transition audit.

These tests stand up an in-memory SQLite engine and exercise the
:func:`src.application.transition_log.record` writer plus the two
:func:`src.services.pipeline_state.transition_session` /
:func:`transition_task_log` CAS helpers that now persist the audit
row in the same transaction as the business UPDATE. They cover:

- The standalone ``record`` writer contract (single INSERT, no
  commit, ``ValueError`` on bad aggregate_type / empty source).
- The CAS helpers' success / lost-race / illegal-transition paths:
  exactly one audit row per applied CAS, zero audit rows per
  non-applied CAS, ``detail_json`` still updated on the inline
  backward-compat path.
- The 7-day ``task_log`` cleanup surrogate: deleting a ``TaskLog``
  NULLs the audit row's ``task_log_id`` but keeps the row itself
  (the append-only history outlives the cleanup, just as
  ``llm_usage_log`` and ``daily_summary_generation_attempt`` do).

PostgreSQL-specific behaviour (CHECK constraint, partial unique,
concurrent INSERT, FK ``ON DELETE SET NULL`` against a real PG
schema) lives in
``tests/integration/test_pipeline_transition_log_postgres.py``.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

import src.db.base  # noqa: F401  (registers PipelineTransitionLog on Base.metadata)
from src.application.transition_log import (
    AGGREGATE_TYPE_TASK_LOG,
    AGGREGATE_TYPE_VIDEO_SESSION,
)
from src.application.transition_log import (
    record as record_transition_audit,
)
from src.db.base_class import Base
from src.models.pipeline_transition_log import PipelineTransitionLog
from src.models.task_log import TaskLog
from src.models.video_session import VideoSession
from src.models.video_source import VideoSource
from src.services.pipeline_constants import SessionAnalysisStatus, TaskStatus, TaskType
from src.services.pipeline_state import (
    PipelineTransitionConflict,
    transition_session,
    transition_task_log,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_session() -> Session:
    """Return a function-scoped SQLite session bound to an in-memory engine.

    ``Base.metadata.create_all`` stands up the full schema (including the
    new ``pipeline_transition_log`` table with its CHECK constraint
    and indexes) so the unit tests exercise the same column / constraint
    contract as production — just against an in-memory SQLite engine
    rather than PostgreSQL.
    """
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    session = factory()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _commit(db: Session) -> None:
    """Commit the session so writes are visible to follow-up queries."""
    db.commit()


def _seed_source(db: Session) -> VideoSource:
    """Create a minimal ``VideoSource`` and flush so FKs to it work."""
    source = VideoSource(
        source_name="transition-audit",
        camera_name="transition-audit",
        location_name="test",
        source_type="local_directory",
        config_json={"root_path": "/tmp"},
        enabled=True,
    )
    db.add(source)
    db.flush()
    return source


def _seed_session(db: Session, source: VideoSource, status: SessionAnalysisStatus) -> VideoSession:
    """Create a minimal ``VideoSession`` and commit so it has an ``id``."""
    session = VideoSession(
        source_id=source.id,
        session_start_time=datetime(2026, 9, 2, 0, 0, 0, tzinfo=timezone.utc),
        session_end_time=datetime(2026, 9, 2, 0, 5, 0, tzinfo=timezone.utc),
        total_duration_seconds=300,
        analysis_status=status,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


def _seed_task_log(db: Session, status: TaskStatus = TaskStatus.PENDING) -> TaskLog:
    """Create a minimal ``TaskLog`` and commit so it has an ``id``."""
    task_log = TaskLog(task_type=TaskType.SESSION_ANALYSIS, status=status)
    db.add(task_log)
    db.commit()
    db.refresh(task_log)
    return task_log


# ---------------------------------------------------------------------------
# record() writer contract
# ---------------------------------------------------------------------------


def test_record_inserts_exactly_one_audit_row(db_session: Session) -> None:
    """A single ``record(...)`` call adds one row to the session;
    the caller owns the commit."""
    record_transition_audit(
        db_session,
        aggregate_type=AGGREGATE_TYPE_VIDEO_SESSION,
        aggregate_id=42,
        from_status="open",
        to_status="sealed",
        reason="scan_completed",
        source="unit_test",
        task_log_id=None,
    )
    _commit(db_session)

    rows = db_session.query(PipelineTransitionLog).all()
    assert len(rows) == 1
    row = rows[0]
    assert row.aggregate_type == "VideoSession"
    assert row.aggregate_id == 42
    assert row.from_status == "open"
    assert row.to_status == "sealed"
    assert row.reason == "scan_completed"
    assert row.source == "unit_test"
    assert row.task_log_id is None
    # ``occurred_at`` defaulted server-side; SQLite stores naive
    # datetimes for ``DateTime(timezone=True)`` columns, so the
    # ``occurred_at`` value will not carry a tz — only round-trip
    # presence is asserted here.
    assert row.occurred_at is not None


def test_record_rejects_unknown_aggregate_type(db_session: Session) -> None:
    """A typo'd ``aggregate_type`` literal raises ``ValueError``
    *before* the INSERT so the failure mode is loud."""
    with pytest.raises(ValueError, match="aggregate_type"):
        record_transition_audit(
            db_session,
            aggregate_type="OutboxEvent",
            aggregate_id=1,
            from_status="pending",
            to_status="published",
            reason=None,
            source="unit_test",
        )
    assert db_session.query(PipelineTransitionLog).count() == 0


def test_record_rejects_empty_source(db_session: Session) -> None:
    """``source`` is a required non-empty string; an empty value
    raises ``ValueError`` (the CHECK constraint in the migration does
    not enforce it, so the application writer owns it)."""
    with pytest.raises(ValueError, match="source"):
        record_transition_audit(
            db_session,
            aggregate_type=AGGREGATE_TYPE_TASK_LOG,
            aggregate_id=1,
            from_status="pending",
            to_status="running",
            reason="start",
            source="",
        )
    assert db_session.query(PipelineTransitionLog).count() == 0


# ---------------------------------------------------------------------------
# transition_session — applied CAS writes exactly one audit row
# ---------------------------------------------------------------------------


def test_transition_session_writes_exactly_one_audit_row_on_success(
    db_session: Session,
) -> None:
    """A successful ``transition_session`` CAS writes exactly one
    ``PipelineTransitionLog`` row in the same transaction; the inline
    ``task_log.detail_json["transition"]`` embed is also updated."""
    source = _seed_source(db_session)
    session = _seed_session(db_session, source, SessionAnalysisStatus.OPEN)
    task_log = _seed_task_log(db_session)

    result = transition_session(
        db_session,
        session.id,
        SessionAnalysisStatus.OPEN,
        SessionAnalysisStatus.SEALED,
        reason="full_scan_completed",
        source="seal_all_open",
        task_log=task_log,
    )
    _commit(db_session)

    assert result.applied is True
    rows = db_session.query(PipelineTransitionLog).all()
    assert len(rows) == 1, "exactly one audit row per successful CAS"
    row = rows[0]
    assert row.aggregate_type == AGGREGATE_TYPE_VIDEO_SESSION
    assert row.aggregate_id == session.id
    assert row.from_status == "open"
    assert row.to_status == "sealed"
    assert row.reason == "full_scan_completed"
    assert row.source == "seal_all_open"
    assert row.task_log_id == task_log.id

    # Backward-compat: the inline detail_json embed is still updated.
    db_session.refresh(task_log)
    assert task_log.detail_json["transition"] == {
        "from": "open",
        "to": "sealed",
        "reason": "full_scan_completed",
        "source": "seal_all_open",
    }


def test_transition_session_lost_race_writes_no_audit_row(
    db_session: Session,
) -> None:
    """A CAS that does not match the ``from_status`` writes **no**
    audit row (lost race). The ``from_status`` filter in the UPDATE
    is the gate; the audit write is gated on the same condition."""
    source = _seed_source(db_session)
    session = _seed_session(db_session, source, SessionAnalysisStatus.OPEN)
    task_log = _seed_task_log(db_session)

    # Drive the session into ``sealed`` behind the helper's back, then
    # ask the helper to do ``open -> sealed``: the ``from_status``
    # filter does not match and the CAS UPDATE matches zero rows.
    session.analysis_status = SessionAnalysisStatus.SEALED
    db_session.flush()

    result = transition_session(
        db_session,
        session.id,
        SessionAnalysisStatus.OPEN,
        SessionAnalysisStatus.SEALED,
        reason="stale_retry",
        source="unit_test",
        task_log=task_log,
    )
    _commit(db_session)

    assert result.applied is False
    assert db_session.query(PipelineTransitionLog).count() == 0


# ---------------------------------------------------------------------------
# transition_task_log — applied CAS writes exactly one audit row
# ---------------------------------------------------------------------------


def test_transition_task_log_writes_audit_row_on_success(
    db_session: Session,
) -> None:
    """A successful ``transition_task_log`` CAS writes exactly one
    audit row correlated with the ``TaskLog`` that just transitioned."""
    task_log = _seed_task_log(db_session, status=TaskStatus.PENDING)

    result = transition_task_log(
        db_session,
        task_log.id,
        TaskStatus.PENDING,
        TaskStatus.RUNNING,
        reason="start",
        source="unit_test",
    )
    _commit(db_session)

    assert result.applied is True
    rows = db_session.query(PipelineTransitionLog).all()
    assert len(rows) == 1
    row = rows[0]
    assert row.aggregate_type == AGGREGATE_TYPE_TASK_LOG
    assert row.aggregate_id == task_log.id
    assert row.from_status == "pending"
    assert row.to_status == "running"
    assert row.reason == "start"
    assert row.source == "unit_test"
    assert row.task_log_id == task_log.id


def test_transition_task_log_lost_race_writes_no_audit_row(
    db_session: Session,
) -> None:
    """A ``transition_task_log`` whose ``from_status`` filter does
    not match (or the row is already in another state / missing)
    writes no audit row."""
    task_log = _seed_task_log(db_session, status=TaskStatus.RUNNING)
    # Drive the row into ``success`` behind the helper's back; the
    # CAS filter ``status == 'running'`` then matches zero rows.
    task_log.status = TaskStatus.SUCCESS
    db_session.flush()

    result = transition_task_log(
        db_session,
        task_log.id,
        TaskStatus.RUNNING,
        TaskStatus.SUCCESS,
        reason="stale_retry",
        source="unit_test",
    )
    _commit(db_session)

    assert result.applied is False
    assert db_session.query(PipelineTransitionLog).count() == 0


# ---------------------------------------------------------------------------
# Illegal transitions — no audit row written, but raises / no-ops cleanly
# ---------------------------------------------------------------------------


def test_illegal_session_transition_raises_and_writes_no_audit(
    db_session: Session,
) -> None:
    """An illegal ``VideoSession`` transition raises
    :class:`PipelineTransitionConflict` before any database work
    happens — no audit row is written, no ``VideoSession`` row is
    updated."""
    source = _seed_source(db_session)
    session = _seed_session(db_session, source, SessionAnalysisStatus.OPEN)

    with pytest.raises(PipelineTransitionConflict):
        transition_session(
            db_session,
            session.id,
            SessionAnalysisStatus.OPEN,
            SessionAnalysisStatus.ANALYZING,
            reason="illegal",
            source="unit_test",
        )
    _commit(db_session)

    assert db_session.query(PipelineTransitionLog).count() == 0
    db_session.refresh(session)
    # Underlying row is untouched.
    assert session.analysis_status == SessionAnalysisStatus.OPEN


def test_illegal_task_transition_raises_and_writes_no_audit(
    db_session: Session,
) -> None:
    """An illegal ``TaskLog`` transition raises
    :class:`PipelineTransitionConflict` before any database work."""
    task_log = _seed_task_log(db_session, status=TaskStatus.SUCCESS)

    with pytest.raises(PipelineTransitionConflict):
        transition_task_log(
            db_session,
            task_log.id,
            TaskStatus.SUCCESS,
            TaskStatus.PENDING,
            reason="illegal",
            source="unit_test",
        )
    _commit(db_session)

    assert db_session.query(PipelineTransitionLog).count() == 0
    db_session.refresh(task_log)
    assert task_log.status == TaskStatus.SUCCESS


# ---------------------------------------------------------------------------
# Field population
# ---------------------------------------------------------------------------


def test_transition_audit_has_all_fields_populated(db_session: Session) -> None:
    """Every required field of the audit row is populated after a
    successful CAS, and the optionals (``reason``,
    ``task_log_id``) reflect the call-site values; the row's
    ``id`` is server-assigned (not None) and ``occurred_at`` is
    populated by the ``NOW()`` default."""
    source = _seed_source(db_session)
    session = _seed_session(db_session, source, SessionAnalysisStatus.OPEN)

    # ``task_log=None`` exercises the no-driving-TaskLog branch on
    # ``transition_session``: the audit row records ``task_log_id``
    # as NULL but everything else is populated.
    result = transition_session(
        db_session,
        session.id,
        SessionAnalysisStatus.OPEN,
        SessionAnalysisStatus.SEALED,
        reason="seal_with_no_driver",
        source="seal_all_open",
        task_log=None,
    )
    _commit(db_session)

    assert result.applied is True
    row = db_session.query(PipelineTransitionLog).one()
    assert row.id is not None
    assert row.aggregate_type == AGGREGATE_TYPE_VIDEO_SESSION
    assert row.aggregate_id == session.id
    assert row.from_status == "open"
    assert row.to_status == "sealed"
    assert row.reason == "seal_with_no_driver"
    assert row.source == "seal_all_open"
    assert row.task_log_id is None
    assert row.occurred_at is not None


# ---------------------------------------------------------------------------
# Cleanup survival — ``TaskLog`` deletion nulls the audit FK, keeps history
# ---------------------------------------------------------------------------


def test_cleanup_task_log_nullifies_fk_but_keeps_transition_audit(
    db_session: Session,
) -> None:
    """The 7-day ``task_log`` cleanup surrogate (a plain ``DELETE``
    on an in-memory SQLite stand-in) must NOT delete the audit row.
    The FK is ``ON DELETE SET NULL`` so the row's ``task_log_id``
    goes to ``NULL`` and the audit history survives.

    SQLite requires the FK constraint to be explicitly enabled via
    the connection's ``PRAGMA foreign_keys = ON``; the SQLAlchemy
    SQLite dialect sets the pragma on every connection from
    3.6.19 onwards, but we set it again here so the test does not
    silently regress if the engine-level default changes.
    """
    from sqlalchemy import event

    engine = create_engine("sqlite+pysqlite:///:memory:")
    # Belt-and-braces: enable foreign keys at the connection level.
    event.listen(engine, "connect", _enable_sqlite_foreign_keys)
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    db = factory()
    try:
        source = _seed_source(db)
        session = _seed_session(db, source, SessionAnalysisStatus.OPEN)
        task_log = _seed_task_log(db)

        # Drive a transition that records an audit row correlated
        # with the ``TaskLog``.
        applied = transition_session(
            db,
            session.id,
            SessionAnalysisStatus.OPEN,
            SessionAnalysisStatus.SEALED,
            reason="before_cleanup",
            source="unit_test",
            task_log=task_log,
        )
        assert applied.applied is True
        _commit(db)

        # Surrogate for the 7-day ``task_log`` cleanup: delete the
        # TaskLog row directly. The migration declares the FK as
        # ``ON DELETE SET NULL``.
        db.query(TaskLog).filter(TaskLog.id == task_log.id).delete(synchronize_session=False)
        _commit(db)

        # Audit row must survive with ``task_log_id`` NULL.
        rows = db.query(PipelineTransitionLog).all()
        assert len(rows) == 1, "audit row must outlive the TaskLog cleanup"
        assert rows[0].id is not None
        assert rows[0].aggregate_id == session.id
        assert rows[0].task_log_id is None
    finally:
        db.close()
        engine.dispose()


def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record):
    """Enable SQLite FK enforcement on every new connection.

    SQLite does NOT enforce foreign keys by default; SQLAlchemy's
    modern SQLite dialect enables them, but the in-memory engine
    used in this test is created with the bare ``sqlite+pysqlite``
    URL so we set the pragma explicitly to make the FK SET NULL
    behaviour reliable.
    """
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys = ON")
    cursor.close()
