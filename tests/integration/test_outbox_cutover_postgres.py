"""PostgreSQL integration tests for the Todo 14 outbox cutover.

These tests run against a real PostgreSQL schema migrated to the
current head (including the ``outbox_event`` table from migration
``20260902_0019``). They cover the consumer-side invariants the cutover
introduces:

- the dispatcher + outbox commit atomically; a rollback after the
  dispatcher returns leaves zero rows in both tables;
- the consumer-side ``bind_or_create_running_task_log(queue_task_id=event_id, ...)``
  seam matches the producer-side ``TaskLog.queue_task_id``;
- duplicate publishes with the same ``event_id`` are short-circuited
  by the consumer's terminal-status guard (``skipped=True, reason=stale_message``);
- two parallel dispatcher transactions with the same dedupe key produce
  exactly one ``TaskLog`` + one ``OutboxEvent`` pair (the partial
  unique index on ``(task_log_id) WHERE status='pending'`` is the
  structural safety net);
- the broker-side failure (``send_task`` raising) does not roll back
  the dispatch transaction; the ``OutboxEvent`` stays ``pending`` and
  the publisher's retry / terminal paths take over.

PG test isolation note
======================

The ``postgres_migrated_engine`` fixture is session-scoped, so all
tests in this file share one schema. An autouse fixture
(``_truncate_outbox_tables``) wipes ``outbox_event`` and ``task_log``
between tests.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text as sql_text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from src.application.outbox import contracts as outbox_contracts
from src.application.outbox.contracts import OutboxStatus
from src.application.outbox.publisher import (
    PUBLISHER_LEASE_SECONDS,
    PUBLISHER_MAX_ATTEMPTS,
    BrokerPort,
    OutboxPublisher,
    PublisherConfig,
)
from src.application.pipeline.commands import (
    AnalyzeSessionCommand,
    GenerateDailySummaryCommand,
    SendWebhookCommand,
    SessionBuildCommand,
)
from src.infrastructure.tasks.celery_dispatcher import CeleryTaskDispatcher
from src.models.outbox import OutboxEvent as OutboxEventRow
from src.models.task_log import TaskLog
from src.services.pipeline_constants import ScanMode, TaskStatus, TaskType
from src.services.task_dispatch_control import (
    bind_or_create_running_task_log,
    create_pending_task_log,
)

pytestmark = pytest.mark.postgres


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_emitted_event_ids() -> None:
    outbox_contracts._reset_emitted_event_ids_for_testing()


@pytest.fixture(autouse=True)
def _truncate_outbox_tables(postgres_migrated_engine: Engine) -> None:
    """Wipe ``outbox_event`` and ``task_log`` between tests."""
    with postgres_migrated_engine.begin() as conn:
        conn.execute(sql_text("TRUNCATE TABLE outbox_event RESTART IDENTITY CASCADE"))
        conn.execute(sql_text("TRUNCATE TABLE task_log RESTART IDENTITY CASCADE"))


def _session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autocommit=False, autoflush=False)


# ---------------------------------------------------------------------------
# Atomicity
# ---------------------------------------------------------------------------


def test_dispatcher_then_consumer_picks_up_via_event_id(
    postgres_migrated_engine: Engine,
) -> None:
    """The dispatcher commits ``TaskLog`` + ``OutboxEvent`` in one tx.

    The consumer-side ``bind_or_create_running_task_log(queue_task_id=event_id, ...)``
    seam binds to the same row that the publisher would publish, by
    reusing ``TaskLog.queue_task_id == str(OutboxEvent.event_id)``.
    """
    factory = _session_factory(postgres_migrated_engine)
    db = factory()
    try:
        task_id = CeleryTaskDispatcher().dispatch_session_build(
            db,
            SessionBuildCommand(source_id=11, scan_mode=ScanMode.HOT),
        )
        db.commit()
        task_log_id = (
            db.query(TaskLog)
            .filter(TaskLog.task_type == TaskType.SESSION_BUILD, TaskLog.task_target_id == 11)
            .one()
            .id
        )
    finally:
        db.close()

    assert task_id is not None

    verify = Session(postgres_migrated_engine)
    try:
        outbox_row = (
            verify.query(OutboxEventRow).filter(OutboxEventRow.task_log_id == task_log_id).one()
        )
        assert outbox_row.event_id is not None
        assert str(outbox_row.event_id) == task_id

        bound = bind_or_create_running_task_log(
            verify,
            queue_task_id=task_id,
            task_type=TaskType.SESSION_BUILD,
            task_target_id=11,
            detail_json={"scan_mode": ScanMode.HOT, "source_id": 11},
        )
        verify.commit()
        assert bound is not None
        assert bound.id == task_log_id
        assert bound.status == TaskStatus.RUNNING
        assert bound.queue_task_id == task_id
    finally:
        verify.close()


def test_double_delivery_consumer_short_circuits(
    postgres_migrated_engine: Engine,
) -> None:
    """A second delivery with the same ``task_id`` short-circuits to
    ``None`` when the row has already reached a terminal state."""
    factory = _session_factory(postgres_migrated_engine)
    db = factory()
    try:
        task_id = CeleryTaskDispatcher().dispatch_analyze_session(
            db,
            AnalyzeSessionCommand(session_id=22, priority=ScanMode.HOT),
        )
        db.commit()
    finally:
        db.close()

    verify = Session(postgres_migrated_engine)
    try:
        bound_first = bind_or_create_running_task_log(
            verify,
            queue_task_id=task_id,
            task_type=TaskType.SESSION_ANALYSIS,
            task_target_id=22,
            detail_json={"priority": ScanMode.HOT},
        )
        verify.commit()
        assert bound_first is not None

        # Finalize the row.
        bound_first.status = TaskStatus.SUCCESS
        bound_first.finished_at = datetime.now(tz=timezone.utc)
        verify.commit()
        verify.expire_all()

        # Re-delivery with the same queue_task_id short-circuits to None.
        bound_second = bind_or_create_running_task_log(
            verify,
            queue_task_id=task_id,
            task_type=TaskType.SESSION_ANALYSIS,
            task_target_id=22,
            detail_json={"priority": ScanMode.HOT},
        )
        verify.commit()
        assert bound_second is None
    finally:
        verify.close()


def test_rollback_during_dispatch_leaves_no_outbox_row(
    postgres_migrated_engine: Engine,
) -> None:
    """A simulated caller rollback after the dispatcher returns leaves
    zero rows in both tables — the atomicity contract holds on PG."""
    factory = _session_factory(postgres_migrated_engine)
    db = factory()
    try:
        CeleryTaskDispatcher().dispatch_generate_daily_summary(
            db,
            GenerateDailySummaryCommand(target_date_str="2026-09-02"),
        )
        db.rollback()
    finally:
        db.close()

    verify = Session(postgres_migrated_engine)
    try:
        assert verify.query(TaskLog).count() == 0
        assert verify.query(OutboxEventRow).count() == 0
    finally:
        verify.close()


# ---------------------------------------------------------------------------
# Concurrency: partial unique index as the dedupe safety net
# ---------------------------------------------------------------------------


def test_concurrent_dispatch_dedupes_via_partial_unique(
    postgres_migrated_engine: Engine,
) -> None:
    """Two parallel dispatcher transactions for the same dedupe key
    produce exactly one ``TaskLog`` + one ``OutboxEvent`` pair.

    The partial unique index on ``(task_log_id) WHERE status='pending'``
    is the structural safety net the dispatcher relies on (the
    repository's ``INSERT … ON CONFLICT DO NOTHING`` falls back to the
    existing row).
    """

    def _dispatch_one(_: int) -> None:
        db = Session(postgres_migrated_engine)
        try:
            CeleryTaskDispatcher().dispatch_session_build(
                db,
                SessionBuildCommand(source_id=33, scan_mode=ScanMode.HOT),
            )
            db.commit()
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(_dispatch_one, range(2)))

    verify = Session(postgres_migrated_engine)
    try:
        task_logs = (
            verify.query(TaskLog)
            .filter(
                TaskLog.task_type == TaskType.SESSION_BUILD,
                TaskLog.task_target_id == 33,
            )
            .all()
        )
        outbox_rows = (
            verify.query(OutboxEventRow)
            .filter(
                OutboxEventRow.status == OutboxStatus.PENDING.value,
            )
            .all()
        )
        assert len(task_logs) == 1
        assert len(outbox_rows) == 1
        assert outbox_rows[0].task_log_id == task_logs[0].id
    finally:
        verify.close()


# ---------------------------------------------------------------------------
# Publish failure does not affect dispatch
# ---------------------------------------------------------------------------


class _FakeBrokerFailingOnce(BrokerPort):
    """In-memory broker that records one call then raises."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc
        self.sent_calls: list[dict[str, object]] = []

    def send_task(
        self,
        *,
        name: str,
        args: list,
        kwargs: dict,
        queue: str,
        task_id: str,
    ) -> str:
        self.sent_calls.append({"name": name, "task_id": task_id, "queue": queue})
        raise self._exc


def test_publish_failure_does_not_affect_dispatch(
    postgres_migrated_engine: Engine,
) -> None:
    """A broker-side ``ConnectionError`` during publish does NOT roll
    back the dispatcher transaction.

    The dispatcher's job is done the moment the ``TaskLog`` and
    ``OutboxEvent`` are committed. The publisher's retry / terminal
    paths take over from there — the row stays ``pending`` until the
    publisher's lease + backoff math completes.
    """
    factory = _session_factory(postgres_migrated_engine)
    dispatch = factory()
    try:
        task_id = CeleryTaskDispatcher().dispatch_webhook(
            dispatch,
            SendWebhookCommand(
                event_type="test_event",
                payload={"foo": "bar"},
            ),
        )
        dispatch.commit()
        task_log_id = (
            dispatch.query(TaskLog).filter(TaskLog.task_type == "webhook_delivery").one().id
        )
    finally:
        dispatch.close()

    assert task_id is not None

    publish = factory()
    try:
        # Pin next_attempt_at to the past so the publisher claims the row.
        publish.query(OutboxEventRow).filter(OutboxEventRow.task_log_id == task_log_id).update(
            {"next_attempt_at": datetime.now(tz=timezone.utc) - timedelta(seconds=60)}
        )
        publish.commit()

        broker = _FakeBrokerFailingOnce(ConnectionError("redis down"))
        publisher = OutboxPublisher(
            publish,
            PublisherConfig(
                claimed_by="publisher-A",
                lease_seconds=PUBLISHER_LEASE_SECONDS,
                max_attempts=PUBLISHER_MAX_ATTEMPTS,
            ),
            broker=broker,
        )
        delta = publisher.run_once()
        publish.commit()
    finally:
        publish.close()

    assert delta.retryable_failures == 1
    assert delta.published == 0

    verify = Session(postgres_migrated_engine)
    try:
        outbox_row = (
            verify.query(OutboxEventRow).filter(OutboxEventRow.task_log_id == task_log_id).one()
        )
        assert outbox_row.status == OutboxStatus.PENDING.value
        assert outbox_row.attempt_count == 1
        assert outbox_row.last_error is not None
        assert "ConnectionError" in outbox_row.last_error

        task_log_row = verify.query(TaskLog).filter(TaskLog.id == task_log_id).one()
        # TaskLog survives the broker failure unchanged.
        assert task_log_row.status == TaskStatus.PENDING.value
        assert task_log_row.queue_task_id == str(outbox_row.event_id)
    finally:
        verify.close()


# ---------------------------------------------------------------------------
# HOT / FULL precedence on PG (smoke test of the supersession path)
# ---------------------------------------------------------------------------


def test_dispatch_session_build_full_supersedes_hot_on_postgres(
    postgres_migrated_engine: Engine,
) -> None:
    """A FULL dispatch supersedes an existing HOT and creates a new
    outbox row keyed by the new ``queue_task_id``."""
    factory = _session_factory(postgres_migrated_engine)
    db = factory()
    try:
        hot_log, hot_created = create_pending_task_log(
            db,
            task_type=TaskType.SESSION_BUILD,
            task_target_id=44,
            detail_json={"scan_mode": ScanMode.HOT, "source_id": 44},
        )
        assert hot_created is True
        db.commit()
        hot_log_id = hot_log.id

        new_task_id = CeleryTaskDispatcher().dispatch_session_build(
            db,
            SessionBuildCommand(source_id=44, scan_mode=ScanMode.FULL),
        )
        db.commit()
    finally:
        db.close()

    assert new_task_id is not None

    verify = Session(postgres_migrated_engine)
    try:
        hot_log = verify.query(TaskLog).filter(TaskLog.id == hot_log_id).one()
        full_log = (
            verify.query(TaskLog)
            .filter(
                TaskLog.task_type == TaskType.SESSION_BUILD,
                TaskLog.task_target_id == 44,
                TaskLog.status == TaskStatus.PENDING,
            )
            .one()
        )
        outbox_row = (
            verify.query(OutboxEventRow).filter(OutboxEventRow.task_log_id == full_log.id).one()
        )
        assert hot_log.status == TaskStatus.CANCELLED
        assert full_log.detail_json["scan_mode"] == ScanMode.FULL.value
        assert full_log.queue_task_id == new_task_id
        assert str(outbox_row.event_id) == new_task_id
    finally:
        verify.close()
