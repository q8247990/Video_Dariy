"""PostgreSQL integration tests for the append-only pipeline transition audit.

These tests exercise the persistence layer on a real PostgreSQL
database that has been migrated to the new head (including the new
``pipeline_transition_log`` table from migration ``20260902_0021``).
They cover the behaviors the SQLite unit tests cannot:

- Replaying a full ``VideoSession`` state chain via the audit rows
  (every successful CAS has a row, and the ``occurred_at`` order
  preserves the chain).
- A cancelled TaskLog cannot drive a transition — the
  ``cancel_requested`` short-circuit returns ``applied=False`` and
  no audit row is written.
- The 7-day ``task_log`` cleanup surrogate (``DELETE FROM task_log``)
  NULLs the audit row's ``task_log_id`` but keeps the row itself.
- The audit row is INSERT-only at the application layer — running
  ``transition_session`` twice writes two distinct rows, not an
  update.
- The FK to ``task_log`` is nullable, ``ON DELETE SET NULL``, and
  the CHECK constraint on ``aggregate_type`` rejects unknown values.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import text as sql_text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.models.pipeline_transition_log import PipelineTransitionLog
from src.models.task_log import TaskLog
from src.models.video_session import VideoSession
from src.models.video_source import VideoSource
from src.services.pipeline_constants import SessionAnalysisStatus, TaskStatus, TaskType
from src.services.pipeline_state import (
    transition_session,
    transition_task_log,
)

pytestmark = pytest.mark.postgres


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _truncate_transition_tables(postgres_migrated_engine: Engine) -> None:
    """Truncate the dependency tree before each test.

    ``postgres_migrated_engine`` is session-scoped so all PG tests in
    this run share one schema; rows left by an earlier test would
    poison the assertions of later tests. The FK direction is
    ``pipeline_transition_log.task_log_id`` → ``task_log.id`` with
    ``ON DELETE SET NULL``; deleting ``task_log`` does not cascade the
    audit rows away, so the order does not matter. ``CASCADE`` on
    ``pipeline_transition_log`` keeps the table empty for the next
    test.
    """
    with postgres_migrated_engine.begin() as conn:
        conn.execute(sql_text("TRUNCATE TABLE pipeline_transition_log RESTART IDENTITY CASCADE"))
        conn.execute(sql_text("TRUNCATE TABLE task_log RESTART IDENTITY CASCADE"))
        conn.execute(sql_text("TRUNCATE TABLE video_session RESTART IDENTITY CASCADE"))
        conn.execute(sql_text("TRUNCATE TABLE video_source RESTART IDENTITY CASCADE"))


def _commit(db: Session) -> None:
    """Commit the session so writes are visible across sessions."""
    db.commit()


def _new_source(db: Session) -> VideoSource:
    """Insert a ``VideoSource`` and commit so it has a server-assigned ``id``."""
    source = VideoSource(
        source_name="transition-audit-pg",
        camera_name="transition-audit-pg",
        location_name="test",
        source_type="local_directory",
        config_json={"root_path": "/tmp"},
        enabled=True,
    )
    db.add(source)
    db.commit()
    db.refresh(source)
    return source


def _new_session(
    db: Session,
    source: VideoSource,
    status: SessionAnalysisStatus = SessionAnalysisStatus.OPEN,
) -> VideoSession:
    """Insert a ``VideoSession`` and commit so it has a server-assigned ``id``."""
    video_session = VideoSession(
        source_id=source.id,
        session_start_time=datetime.now(timezone.utc),
        session_end_time=datetime.now(timezone.utc),
        total_duration_seconds=300,
        analysis_status=status,
    )
    db.add(video_session)
    db.commit()
    db.refresh(video_session)
    return video_session


def _new_task_log(
    db: Session,
    *,
    status: TaskStatus = TaskStatus.PENDING,
    cancel_requested: bool = False,
) -> TaskLog:
    """Insert a ``TaskLog`` and commit so it has a server-assigned ``id``."""
    task_log = TaskLog(
        task_type=TaskType.SESSION_ANALYSIS,
        status=status,
        cancel_requested=cancel_requested,
    )
    db.add(task_log)
    db.commit()
    db.refresh(task_log)
    return task_log


# ---------------------------------------------------------------------------
# Full Session state chain replays via audit rows
# ---------------------------------------------------------------------------


def test_full_session_state_chain_replays_via_audit_rows(
    postgres_migrated_engine: Engine,
) -> None:
    """A full ``VideoSession`` state chain (OPEN → SEALED → ANALYZING →
    SUCCESS) writes three audit rows in order — one for each
    applied CAS — all correlated to the same ``aggregate_id`` so the
    audit query can replay the chain from oldest to newest."""

    video_session_id: int
    first_audit_task_log_id: object
    with Session(postgres_migrated_engine) as session:
        source = _new_source(session)
        video_session = _new_session(session, source)
        video_session_id = video_session.id
        task_log = _new_task_log(session)

        # Step 1: OPEN → SEALED, no driver task_log.
        applied = transition_session(
            session,
            video_session.id,
            SessionAnalysisStatus.OPEN,
            SessionAnalysisStatus.SEALED,
            reason="full_scan_completed",
            source="seal_all_open",
        )
        assert applied.applied is True
        _commit(session)

        # Step 2: SEALED → ANALYZING, task_log driving the transition.
        applied = transition_session(
            session,
            video_session.id,
            SessionAnalysisStatus.SEALED,
            SessionAnalysisStatus.ANALYZING,
            reason="analysis_claim",
            source="claim_session_for_analysis",
            task_log=task_log,
        )
        assert applied.applied is True
        _commit(session)

        # Step 3: ANALYZING → SUCCESS, same task_log.
        applied = transition_session(
            session,
            video_session.id,
            SessionAnalysisStatus.ANALYZING,
            SessionAnalysisStatus.SUCCESS,
            reason="analysis_completed",
            source="analyze_session_task",
            task_log=task_log,
        )
        assert applied.applied is True
        _commit(session)

        first_audit_task_log_id = (
            session.query(PipelineTransitionLog)
            .filter(
                PipelineTransitionLog.aggregate_type == "VideoSession",
                PipelineTransitionLog.aggregate_id == video_session_id,
                PipelineTransitionLog.from_status == "sealed",
            )
            .one()
            .task_log_id
        )

    with Session(postgres_migrated_engine) as verify:
        rows = (
            verify.query(PipelineTransitionLog)
            .filter(
                PipelineTransitionLog.aggregate_type == "VideoSession",
                PipelineTransitionLog.aggregate_id == video_session_id,
            )
            .order_by(PipelineTransitionLog.occurred_at.asc(), PipelineTransitionLog.id.asc())
            .all()
        )
        assert len(rows) == 3, [(r.from_status, r.to_status) for r in rows]
        assert [row.from_status for row in rows] == ["open", "sealed", "analyzing"]
        assert [row.to_status for row in rows] == ["sealed", "analyzing", "success"]
        assert [row.reason for row in rows] == [
            "full_scan_completed",
            "analysis_claim",
            "analysis_completed",
        ]
        # The first audit row has no driving task_log_id. The next two
        # are correlated to the same ``task_log.id``.
        assert rows[0].task_log_id is None
        assert rows[1].task_log_id == first_audit_task_log_id
        assert rows[2].task_log_id == first_audit_task_log_id


# ---------------------------------------------------------------------------
# Cancel / no-false-audit
# ---------------------------------------------------------------------------


def test_cancel_then_complete_no_false_audit(
    postgres_migrated_engine: Engine,
) -> None:
    """A cancellation marks the ``TaskLog`` ``cancel_requested=True``,
    but the row's status is still ``RUNNING`` (the cancel check is on
    the *next* transition). The session itself is in ``SEALED``
    state. Calling ``transition_session`` with the now-cancelled
    TaskLog returns ``applied=False`` (the ``cancel_requested``
    short-circuit fires — checked **before** the CAS UPDATE) — no
    audit row is written and the ``VideoSession`` is not moved out of
    ``SEALED``."""

    with Session(postgres_migrated_engine) as session:
        source = _new_source(session)
        video_session = _new_session(session, source, status=SessionAnalysisStatus.SEALED)
        video_session_id = video_session.id
        cancelled_task_log = _new_task_log(
            session, status=TaskStatus.RUNNING, cancel_requested=True
        )

        # SEALED → ANALYZING with a cancelled driver task_log. The
        # helper short-circuits on ``task_log.cancel_requested`` and
        # never writes the audit row.
        applied = transition_session(
            session,
            video_session.id,
            SessionAnalysisStatus.SEALED,
            SessionAnalysisStatus.ANALYZING,
            reason="cancellation_short_circuit",
            source="analyze_session_task",
            task_log=cancelled_task_log,
        )
        assert applied.applied is False
        _commit(session)
        cancelled_task_log_id = cancelled_task_log.id

    with Session(postgres_migrated_engine) as verify:
        count = verify.query(PipelineTransitionLog).count()
        assert count == 0, "no audit row written for a cancelled short-circuit"
        refreshed_session = verify.query(VideoSession).filter_by(id=video_session_id).one()
        assert refreshed_session.analysis_status == SessionAnalysisStatus.SEALED
        refreshed_task_log = verify.query(TaskLog).filter_by(id=cancelled_task_log_id).one()
        assert refreshed_task_log.cancel_requested is True


# ---------------------------------------------------------------------------
# TaskLog 7-day cleanup: FK SET NULL, history outlives the row
# ---------------------------------------------------------------------------


def test_task_log_7day_cleanup_keeps_audit_rows(
    postgres_migrated_engine: Engine,
) -> None:
    """The 7-day ``TaskLog`` cleanup (a direct ``DELETE`` in this test,
    mirroring ``src.services.maintenance.cleanup_old_task_logs``)
    removes the ``task_log`` row but keeps every audit row that
    referenced it — its ``task_log_id`` goes to ``NULL`` because the FK
    is ``ON DELETE SET NULL``."""

    video_session_id: int
    task_log_id: int
    analyze_audit_id: int
    task_audit_id: int
    seal_audit_id: int

    with Session(postgres_migrated_engine) as session:
        source = _new_source(session)
        video_session = _new_session(session, source)
        video_session_id = video_session.id
        task_log = _new_task_log(session)
        task_log_id = task_log.id

        # OPEN → SEALED, no driver task_log — unrelated audit row,
        # exists for the count assertion at the bottom.
        applied = transition_session(
            session,
            video_session.id,
            SessionAnalysisStatus.OPEN,
            SessionAnalysisStatus.SEALED,
            reason="seal",
            source="unit_test",
        )
        assert applied.applied is True

        # SEALED → ANALYZING with the TaskLog driving the transition.
        applied = transition_session(
            session,
            video_session.id,
            SessionAnalysisStatus.SEALED,
            SessionAnalysisStatus.ANALYZING,
            reason="analysis_claim",
            source="unit_test",
            task_log=task_log,
        )
        assert applied.applied is True

        # PENDING → RUNNING on the task_log itself — writes a
        # ``TaskLog`` aggregate audit row correlated with the same id.
        applied = transition_task_log(
            session,
            task_log.id,
            TaskStatus.PENDING,
            TaskStatus.RUNNING,
            reason="start",
            source="unit_test",
        )
        assert applied.applied is True
        _commit(session)

        # Snapshot the audit-row IDs so the post-delete assertions
        # can re-select them by PK.
        rows = session.query(PipelineTransitionLog).order_by(PipelineTransitionLog.id.asc()).all()
        assert len(rows) == 3
        seal_audit_id = rows[0].id
        analyze_audit_id = rows[1].id
        task_audit_id = rows[2].id

    with Session(postgres_migrated_engine) as deleter:
        deleter.execute(
            sql_text("DELETE FROM task_log WHERE id = :id"),
            {"id": task_log_id},
        )
        deleter.commit()

    with Session(postgres_migrated_engine) as verify:
        # The two audit rows correlated to the deleted TaskLog
        # survive with ``task_log_id`` NULL.
        correlated = (
            verify.query(PipelineTransitionLog)
            .filter(PipelineTransitionLog.id.in_((analyze_audit_id, task_audit_id)))
            .order_by(PipelineTransitionLog.id.asc())
            .all()
        )
        assert len(correlated) == 2, "audit rows must outlive the task_log cleanup"
        assert {row.task_log_id for row in correlated} == {None}

        # The uncorrelated audit row (no task_log_id) survives
        # untouched. ``aggregate_id`` is preserved so a post-mortem
        # replay still works.
        unrelated = verify.query(PipelineTransitionLog).filter_by(id=seal_audit_id).one()
        assert unrelated.task_log_id is None
        assert unrelated.aggregate_id == video_session_id

        # Total: 3 audit rows preserved.
        assert verify.query(PipelineTransitionLog).count() == 3


# ---------------------------------------------------------------------------
# Append-only invariant — UPDATE is rejected at the model layer
# ---------------------------------------------------------------------------


def test_transition_audit_is_append_only_no_update_semantics(
    postgres_migrated_engine: Engine,
) -> None:
    """The audit table is append-only in the application contract —
    :func:`src.application.transition_log.record` issues one INSERT
    per call and nothing else. Two CAS calls against the same
    aggregate therefore write two distinct rows (never an UPDATE).

    Note: the model itself does NOT declare a ``BEFORE UPDATE``
    trigger; the append-only invariant is enforced at the writer
    layer (no UPDATE is ever issued by the application) and at the
    schema layer (no FK / no idempotency key that would require
    UPSERT). The test asserts the application-layer half: two CAS
    calls yield two rows with the correct from / to statuses."""

    video_session_id: int
    with Session(postgres_migrated_engine) as session:
        source = _new_source(session)
        video_session = _new_session(session, source)
        video_session_id = video_session.id

        before = session.query(PipelineTransitionLog).count()

        applied = transition_session(
            session,
            video_session.id,
            SessionAnalysisStatus.OPEN,
            SessionAnalysisStatus.SEALED,
            reason="initial",
            source="unit_test",
        )
        assert applied.applied is True
        _commit(session)
        after = session.query(PipelineTransitionLog).count()
        assert after == before + 1, "exactly one row inserted per CAS, no updates"

        applied = transition_session(
            session,
            video_session.id,
            SessionAnalysisStatus.SEALED,
            SessionAnalysisStatus.ANALYZING,
            reason="resume",
            source="unit_test",
        )
        assert applied.applied is True
        _commit(session)

    with Session(postgres_migrated_engine) as verify:
        rows = (
            verify.query(PipelineTransitionLog)
            .filter(PipelineTransitionLog.aggregate_id == video_session_id)
            .order_by(PipelineTransitionLog.id.asc())
            .all()
        )
        assert len(rows) == 2
        assert [row.from_status for row in rows] == ["open", "sealed"]
        assert [row.to_status for row in rows] == ["sealed", "analyzing"]


# ---------------------------------------------------------------------------
# FK semantics — nullable + ON DELETE SET NULL (and CHECK guard)
# ---------------------------------------------------------------------------


def test_fk_to_task_log_set_null_on_delete_pg(
    postgres_migrated_engine: Engine,
) -> None:
    """The FK to ``task_log`` is nullable, ``ON DELETE SET NULL`` —
    a direct INSERT with ``task_log_id=NULL`` is accepted, and a
    direct ``DELETE FROM task_log`` NULLs the audit's ``task_log_id``
    rather than cascading the audit row away. The CHECK constraint
    on ``aggregate_type`` rejects unknown values."""

    # Direct INSERT with NULL task_log_id is accepted.
    with Session(postgres_migrated_engine) as session:
        session.execute(
            sql_text(
                "INSERT INTO pipeline_transition_log "
                "(aggregate_type, aggregate_id, from_status, to_status, "
                " reason, source, task_log_id, occurred_at, created_at, updated_at) "
                "VALUES ('VideoSession', 1, 'open', 'sealed', 'r', 's', "
                " NULL, now(), now(), now())"
            )
        )
        session.commit()

    # CHECK rejects unknown aggregate_type.
    with Session(postgres_migrated_engine) as session:
        with pytest.raises(IntegrityError):
            session.execute(
                sql_text(
                    "INSERT INTO pipeline_transition_log "
                    "(aggregate_type, aggregate_id, from_status, to_status, "
                    " reason, source, task_log_id, occurred_at, created_at, updated_at) "
                    "VALUES ('OutboxEvent', 1, 'pending', 'published', 'r', 's', "
                    " NULL, now(), now(), now())"
                )
            )
        session.rollback()

    # FK SET NULL: insert two audit rows correlated to the same
    # task_log; delete the task_log; both rows survive with
    # task_log_id NULL.
    task_log_id: int
    with Session(postgres_migrated_engine) as setup:
        task_log = _new_task_log(setup)
        task_log_id = task_log.id
        setup.execute(
            sql_text(
                "INSERT INTO pipeline_transition_log "
                "(aggregate_type, aggregate_id, from_status, to_status, "
                " reason, source, task_log_id, occurred_at, created_at, updated_at) "
                "VALUES (:agg_type, :agg_id, :fs, :ts, :reason, :source, "
                " :task_log_id, now(), now(), now())"
            ),
            [
                {
                    "agg_type": "VideoSession",
                    "agg_id": 1001,
                    "fs": "open",
                    "ts": "sealed",
                    "reason": "r1",
                    "source": "s1",
                    "task_log_id": task_log_id,
                },
                {
                    "agg_type": "VideoSession",
                    "agg_id": 1002,
                    "fs": "open",
                    "ts": "sealed",
                    "reason": "r2",
                    "source": "s2",
                    "task_log_id": task_log_id,
                },
            ],
        )
        setup.commit()

    with Session(postgres_migrated_engine) as deleter:
        deleter.execute(
            sql_text("DELETE FROM task_log WHERE id = :id"),
            {"id": task_log_id},
        )
        deleter.commit()

    with Session(postgres_migrated_engine) as verify:
        survivors = (
            verify.query(PipelineTransitionLog)
            .filter(PipelineTransitionLog.aggregate_id.in_((1001, 1002)))
            .order_by(PipelineTransitionLog.aggregate_id.asc())
            .all()
        )
        assert len(survivors) == 2
        assert {row.task_log_id for row in survivors} == {None}
