"""Unit tests for the Todo 14 outbox cutover.

These tests pin the behavior of
:class:`src.infrastructure.tasks.celery_dispatcher.CeleryTaskDispatcher`
under the new outbox-in-caller-session model:

- The dispatcher never opens its own ``SessionLocal`` and never calls
  ``celery_app.send_task``; it only writes ``TaskLog`` and
  ``OutboxEvent`` to the caller's session.
- A caller ``commit()`` makes both rows visible atomically; a
  ``rollback()`` leaves zero rows behind.
- ``TaskLog.queue_task_id = str(OutboxEvent.event_id)`` so the
  consumer-side ``bind_or_create_running_task_log(queue_task_id=...)``
  idempotency seam (ADR §7) can bind by ``event_id``.
- HOT-over-FULL defers the HOT (no outbox row), FULL-over-HOT
  supersedes and publishes a new outbox row, and same-mode duplicates
  reuse the existing active ``queue_task_id``.
- Webhook dispatches create their own ``TaskLog`` row (no dedupe) but
  still route through the outbox so the FK target is satisfied.

SQLite is used as the unit-test substrate (the PG partial unique index
is exercised by ``tests/integration/test_outbox_cutover_postgres.py``).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import src.db.base  # noqa: F401  (registers all models with Base.metadata)
from src.application.outbox import contracts as outbox_contracts
from src.application.outbox.registry import OutboxCommandRegistry
from src.application.pipeline.commands import (
    AnalyzeSessionCommand,
    GenerateDailySummaryCommand,
    SendWebhookCommand,
    SessionBuildCommand,
)
from src.db.base_class import Base
from src.infrastructure.tasks.celery_dispatcher import CeleryTaskDispatcher
from src.models.outbox import OutboxEvent as OutboxEventRow
from src.models.task_log import TaskLog
from src.services.pipeline_constants import ScanMode, TaskStatus, TaskType
from src.services.task_dispatch_control import create_pending_task_log


@pytest.fixture
def engine() -> Iterator[Engine]:
    """Shared in-memory SQLite engine so multiple sessions see the
    same data; ``StaticPool`` keeps the connection alive across
    ``sessionmaker()`` calls."""
    eng = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autocommit=False, autoflush=False)


@pytest.fixture(autouse=True)
def _reset_outbox_state() -> None:
    outbox_contracts._reset_emitted_event_ids_for_testing()
    OutboxCommandRegistry.reset_for_testing()


def _reset_outbox_state() -> None:
    outbox_contracts._reset_emitted_event_ids_for_testing()
    OutboxCommandRegistry.reset_for_testing()


# ---------------------------------------------------------------------------
# Atomicity: TaskLog + OutboxEvent in one transaction
# ---------------------------------------------------------------------------


def test_dispatcher_writes_task_log_and_outbox_in_one_transaction(
    session_factory: sessionmaker[Session],
) -> None:
    """A single dispatch writes both rows; before commit neither is
    visible to a fresh session, after commit both are."""
    db = session_factory()
    try:
        task_id = CeleryTaskDispatcher().dispatch_session_build(
            db,
            SessionBuildCommand(source_id=1, scan_mode=ScanMode.HOT),
        )
        db.commit()
    finally:
        db.close()

    assert task_id is not None
    assert isinstance(task_id, str)
    assert task_id != ""

    fresh = session_factory()
    try:
        task_log = (
            fresh.query(TaskLog)
            .filter(TaskLog.task_type == TaskType.SESSION_BUILD, TaskLog.task_target_id == 1)
            .one()
        )
        outbox_row = (
            fresh.query(OutboxEventRow).filter(OutboxEventRow.task_log_id == task_log.id).one()
        )
        assert task_log.queue_task_id == str(outbox_row.event_id)
        assert task_log.queue_task_id == task_id
    finally:
        fresh.close()


def test_dispatcher_rollback_leaves_no_rows(session_factory: sessionmaker[Session]) -> None:
    """A caller rollback after the dispatcher returns leaves zero rows
    in both tables — the atomicity contract from ADR §1."""
    db = session_factory()
    try:
        CeleryTaskDispatcher().dispatch_session_build(
            db,
            SessionBuildCommand(source_id=1, scan_mode=ScanMode.HOT),
        )
        db.rollback()
    finally:
        db.close()

    fresh = session_factory()
    try:
        assert fresh.query(TaskLog).count() == 0
        assert fresh.query(OutboxEventRow).count() == 0
    finally:
        fresh.close()


def test_dispatcher_uses_event_id_as_queue_task_id(session_factory: sessionmaker[Session]) -> None:
    """``TaskLog.queue_task_id == str(OutboxEvent.event_id)`` so the
    consumer-side ``bind_or_create_running_task_log(queue_task_id=...)``
    can bind by ``event_id``."""
    db = session_factory()
    try:
        task_id = CeleryTaskDispatcher().dispatch_session_build(
            db,
            SessionBuildCommand(source_id=42, scan_mode=ScanMode.FULL),
        )
        db.commit()
    finally:
        db.close()

    fresh = session_factory()
    try:
        task_log = (
            fresh.query(TaskLog)
            .filter(TaskLog.task_type == TaskType.SESSION_BUILD, TaskLog.task_target_id == 42)
            .one()
        )
        outbox_row = (
            fresh.query(OutboxEventRow).filter(OutboxEventRow.task_log_id == task_log.id).one()
        )
        assert task_id == str(outbox_row.event_id)
        assert task_log.queue_task_id == str(outbox_row.event_id)
    finally:
        fresh.close()


# ---------------------------------------------------------------------------
# Dedupe semantics (HOT over FULL, FULL over HOT)
# ---------------------------------------------------------------------------


def test_dispatcher_session_build_hot_then_full_supersedes(
    session_factory: sessionmaker[Session],
) -> None:
    """A FULL dispatch while a HOT is active supersedes the HOT and
    creates a new outbox row keyed by the new ``queue_task_id``."""
    db = session_factory()
    try:
        hot_log, hot_created = create_pending_task_log(
            db,
            task_type=TaskType.SESSION_BUILD,
            task_target_id=1,
            detail_json={"scan_mode": ScanMode.HOT, "source_id": 1},
        )
        assert hot_created is True
        db.commit()
        hot_log_id = hot_log.id

        new_task_id = CeleryTaskDispatcher().dispatch_session_build(
            db,
            SessionBuildCommand(source_id=1, scan_mode=ScanMode.FULL),
        )
        db.commit()
    finally:
        db.close()

    assert new_task_id is not None
    fresh = session_factory()
    try:
        hot_log = fresh.query(TaskLog).filter(TaskLog.id == hot_log_id).one()
        full_log = (
            fresh.query(TaskLog)
            .filter(
                TaskLog.task_type == TaskType.SESSION_BUILD,
                TaskLog.task_target_id == 1,
                TaskLog.status == TaskStatus.PENDING,
            )
            .one()
        )
        outbox_row = (
            fresh.query(OutboxEventRow).filter(OutboxEventRow.task_log_id == full_log.id).one()
        )
        assert hot_log.status == TaskStatus.CANCELLED
        assert full_log.detail_json["scan_mode"] == ScanMode.FULL.value
        assert full_log.queue_task_id == new_task_id
        assert new_task_id == str(outbox_row.event_id)
    finally:
        fresh.close()


def test_dispatcher_session_build_full_then_hot_defers(
    session_factory: sessionmaker[Session],
) -> None:
    """A HOT dispatch while a FULL is active records a deferred
    (skipped) TaskLog and does NOT create an outbox row."""
    db = session_factory()
    try:
        full_log, full_created = create_pending_task_log(
            db,
            task_type=TaskType.SESSION_BUILD,
            task_target_id=2,
            detail_json={"scan_mode": ScanMode.FULL, "source_id": 2},
        )
        assert full_created is True
        db.commit()
        full_log_id = full_log.id

        defer_task_id = CeleryTaskDispatcher().dispatch_session_build(
            db,
            SessionBuildCommand(source_id=2, scan_mode=ScanMode.HOT),
        )
        db.commit()
    finally:
        db.close()

    assert defer_task_id is not None
    fresh = session_factory()
    try:
        full_log = fresh.query(TaskLog).filter(TaskLog.id == full_log_id).one()
        deferred = (
            fresh.query(TaskLog)
            .filter(
                TaskLog.task_type == TaskType.SESSION_BUILD,
                TaskLog.task_target_id == 2,
                TaskLog.status == TaskStatus.SKIPPED,
            )
            .one()
        )
        assert full_log.status == TaskStatus.PENDING
        assert deferred.detail_json["reason"] == "full_scan_in_progress"
        assert defer_task_id == str(deferred.id)
        # No new outbox row was created for the deferred HOT.
        assert (
            fresh.query(OutboxEventRow).filter(OutboxEventRow.task_log_id == deferred.id).count()
            == 0
        )
    finally:
        fresh.close()


def test_dispatcher_duplicate_active_returns_existing_id(
    session_factory: sessionmaker[Session],
) -> None:
    """A second dispatch for the same dedupe key reuses the existing
    ``queue_task_id``; no new outbox row is created."""
    db = session_factory()
    try:
        first_id = CeleryTaskDispatcher().dispatch_session_build(
            db,
            SessionBuildCommand(source_id=3, scan_mode=ScanMode.HOT),
        )
        db.commit()
        second_id = CeleryTaskDispatcher().dispatch_session_build(
            db,
            SessionBuildCommand(source_id=3, scan_mode=ScanMode.HOT),
        )
        db.commit()
    finally:
        db.close()

    assert first_id == second_id
    fresh = session_factory()
    try:
        outbox_count = (
            fresh.query(OutboxEventRow)
            .join(TaskLog, TaskLog.id == OutboxEventRow.task_log_id)
            .filter(TaskLog.task_target_id == 3)
            .count()
        )
        assert outbox_count == 1
    finally:
        fresh.close()


# ---------------------------------------------------------------------------
# Analyzer / summary / webhook payload shapes
# ---------------------------------------------------------------------------


def test_dispatcher_analyze_session_sets_queue_by_priority(
    session_factory: sessionmaker[Session],
) -> None:
    """HOT priority → ``analysis_hot`` queue, FULL → ``analysis_full``."""
    db = session_factory()
    try:
        hot_id = CeleryTaskDispatcher().dispatch_analyze_session(
            db,
            AnalyzeSessionCommand(session_id=10, priority=ScanMode.HOT),
        )
        db.commit()
        full_id = CeleryTaskDispatcher().dispatch_analyze_session(
            db,
            AnalyzeSessionCommand(session_id=11, priority=ScanMode.FULL),
        )
        db.commit()
    finally:
        db.close()

    fresh = session_factory()
    try:
        hot_log = (
            fresh.query(TaskLog)
            .filter(TaskLog.task_target_id == 10, TaskLog.task_type == TaskType.SESSION_ANALYSIS)
            .one()
        )
        full_log = (
            fresh.query(TaskLog)
            .filter(TaskLog.task_target_id == 11, TaskLog.task_type == TaskType.SESSION_ANALYSIS)
            .one()
        )
        hot_outbox = (
            fresh.query(OutboxEventRow).filter(OutboxEventRow.task_log_id == hot_log.id).one()
        )
        full_outbox = (
            fresh.query(OutboxEventRow).filter(OutboxEventRow.task_log_id == full_log.id).one()
        )
        assert hot_id == str(hot_outbox.event_id)
        assert full_id == str(full_outbox.event_id)
        assert hot_outbox.queue == "analysis_hot"
        assert full_outbox.queue == "analysis_full"
        assert hot_outbox.task_name == "src.tasks.analyzer.analyze_session_task"
        assert hot_outbox.args_json == [10]
        assert hot_outbox.kwargs_json == {"priority": "hot"}
    finally:
        fresh.close()


def test_dispatcher_generate_daily_summary_with_target_date_in_args(
    session_factory: sessionmaker[Session],
) -> None:
    """A non-None ``target_date_str`` lands in the broker ``args`` list."""
    db = session_factory()
    try:
        task_id = CeleryTaskDispatcher().dispatch_generate_daily_summary(
            db,
            GenerateDailySummaryCommand(target_date_str="2026-09-02"),
        )
        db.commit()
    finally:
        db.close()

    fresh = session_factory()
    try:
        task_log = (
            fresh.query(TaskLog)
            .filter(TaskLog.task_type == TaskType.DAILY_SUMMARY_GENERATION)
            .one()
        )
        outbox_row = (
            fresh.query(OutboxEventRow).filter(OutboxEventRow.task_log_id == task_log.id).one()
        )
        assert task_id == str(outbox_row.event_id)
        assert outbox_row.args_json == ["2026-09-02"]
        assert outbox_row.task_name == "src.tasks.summarizer.generate_daily_summary_task"
    finally:
        fresh.close()


def test_dispatcher_generate_daily_summary_without_target_date_in_args(
    session_factory: sessionmaker[Session],
) -> None:
    """A None ``target_date_str`` produces an empty ``args`` list."""
    db = session_factory()
    try:
        CeleryTaskDispatcher().dispatch_generate_daily_summary(
            db,
            GenerateDailySummaryCommand(target_date_str=None),
        )
        db.commit()
    finally:
        db.close()

    fresh = session_factory()
    try:
        outbox_row = fresh.query(OutboxEventRow).one()
        assert outbox_row.args_json == []
    finally:
        fresh.close()


def test_dispatcher_webhook_passes_event_type_and_payload_in_kwargs(
    session_factory: sessionmaker[Session],
) -> None:
    """Webhook dispatch wraps ``event_type`` + ``payload`` into the
    outbox ``kwargs``; the broker ``args`` list is empty."""
    db = session_factory()
    try:
        payload = {"date": "2026-09-02", "score": 5}
        task_id = CeleryTaskDispatcher().dispatch_webhook(
            db,
            SendWebhookCommand(event_type="daily_summary_generated", payload=payload),
        )
        db.commit()
    finally:
        db.close()

    fresh = session_factory()
    try:
        outbox_row = fresh.query(OutboxEventRow).one()
        assert task_id == str(outbox_row.event_id)
        assert outbox_row.task_name == "src.tasks.webhook.send_webhook_task"
        assert outbox_row.args_json == []
        assert outbox_row.kwargs_json == {
            "event_type": "daily_summary_generated",
            "payload": {"date": "2026-09-02", "score": 5},
        }
    finally:
        fresh.close()


# ---------------------------------------------------------------------------
# Fake dispatcher signature — protocol satisfaction
# ---------------------------------------------------------------------------


def test_fake_dispatcher_signature_matches_port() -> None:
    """The :class:`FakeTaskDispatcher` accepts ``(db, command)`` for
    every dispatch method and satisfies the runtime-checkable
    :class:`TaskDispatcherPort` Protocol."""
    from src.application.bootstrap_fakes import FakeTaskDispatcher
    from src.application.ports.task_dispatcher import TaskDispatcherPort

    dispatcher = FakeTaskDispatcher()
    sentinel_db = object()

    build_id = dispatcher.dispatch_session_build(
        sentinel_db, SessionBuildCommand(source_id=1, scan_mode=ScanMode.HOT)
    )
    analyze_id = dispatcher.dispatch_analyze_session(
        sentinel_db, AnalyzeSessionCommand(session_id=1)
    )
    summary_id = dispatcher.dispatch_generate_daily_summary(
        sentinel_db, GenerateDailySummaryCommand(target_date_str=None)
    )
    webhook_id = dispatcher.dispatch_webhook(
        sentinel_db,
        SendWebhookCommand(event_type="e", payload={"a": 1}),
    )

    assert isinstance(dispatcher, TaskDispatcherPort)
    assert build_id.startswith("fake-task-")
    assert analyze_id.startswith("fake-task-")
    assert summary_id.startswith("fake-task-")
    assert webhook_id.startswith("fake-task-")
    # The fake recorded the caller's session per dispatch.
    assert dispatcher.dispatched_sessions == [sentinel_db] * 4
