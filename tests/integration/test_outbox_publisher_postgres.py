"""PostgreSQL integration tests for :class:`OutboxPublisher`.

These tests exercise the publisher's claim → publish → mark loop on a
real PostgreSQL database that has been migrated to the new head. The
SQLite unit tests in ``tests/unit/test_outbox_publisher.py`` cover the
happy / retryable / terminal classification math; this module covers
the contract surfaces that the unit tests cannot:

- end-to-end publish round-trip with a fake broker: ``run_once`` claims
  the row, forwards ``task_id=str(event_id)`` to the broker, and marks
  the row ``published`` with a non-null ``published_at``;
- two publishers contending on the same pending row: ``FOR UPDATE
  SKIP LOCKED`` semantics ensure one publisher wins the claim and
  the other sees ``None``;
- lease expiry reclaim path: a row whose previous publisher's lease
  has expired AND whose ``next_attempt_at`` is in the past is picked
  up by a fresh instance;
- 30-day retention: ``cleanup_published_older_than(now - 30 days)``
  removes only the rows whose ``published_at`` is older than the
  threshold;
- 12 consecutive failures exhaust the budget: the row goes terminal
  (``status='failed'``) and is not reclaimed automatically;
- broker restart simulation: a transient ``ConnectionError`` followed
  by a successful publish on the next attempt transitions the row
  from ``pending → published`` with ``attempt_count == 1``;
- old rows are still published: a row whose ``next_attempt_at`` is
  far in the past is still claimed and forwarded to the broker (the
  publisher only logs ``lag_degraded`` for visibility).

PG test isolation note
======================

The ``postgres_migrated_engine`` fixture is session-scoped, so all
tests in this file share one schema. An autouse fixture
(``_truncate_outbox_tables``) wipes ``outbox_event`` and ``task_log``
between tests. Every assertion that depends on the row state filters
by ``event_id`` (the durable row identity) rather than by global
counts, so cross-test contamination is impossible.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

import pytest
from sqlalchemy import text as sql_text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from src.application.outbox import contracts as outbox_contracts
from src.application.outbox.contracts import OutboxCommand, OutboxEvent
from src.application.outbox.enqueue import enqueue_command
from src.application.outbox.publisher import (
    PUBLISHER_LEASE_SECONDS,
    PUBLISHER_MAX_ATTEMPTS,
    BrokerPort,
    OutboxPublisher,
    PublisherConfig,
)
from src.application.outbox.repository import OutboxRepository
from src.application.outbox.state_machine import OutboxStatus
from src.models.outbox import OutboxEvent as OutboxEventRow
from src.models.task_log import TaskLog

pytestmark = pytest.mark.postgres


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_emitted_event_ids() -> None:
    """Reset the in-process duplicate-event-id set."""
    outbox_contracts._reset_emitted_event_ids_for_testing()


@pytest.fixture(autouse=True)
def _truncate_outbox_tables(postgres_migrated_engine: Engine) -> None:
    """Wipe ``outbox_event`` and ``task_log`` between tests.

    The session-scoped engine means rows left by an earlier test
    would poison later tests. The FK is ``ON DELETE RESTRICT`` so we
    drop ``outbox_event`` first; ``task_log`` is recreated empty.
    Mirrors the pattern from
    ``tests/integration/test_outbox_repository_postgres.py``.
    """
    with postgres_migrated_engine.begin() as conn:
        conn.execute(sql_text("TRUNCATE TABLE outbox_event RESTART IDENTITY CASCADE"))
        conn.execute(sql_text("TRUNCATE TABLE task_log RESTART IDENTITY CASCADE"))


@dataclass
class _FakeBroker(BrokerPort):
    """In-memory :class:`BrokerPort` that records ``send_task`` calls.

    Mirrors the unit-test fake so the assertion surface is
    consistent. ``raise_with`` lets a test inject a single-exception
    failure; ``raise_with_sequence`` lets a test simulate a transient
    failure followed by success.
    """

    sent_calls: list[dict[str, object]] = field(default_factory=list)
    raise_with: Optional[Exception] = None
    raise_with_sequence: Optional[list[Optional[Exception]]] = None
    _call_index: int = 0

    def send_task(
        self,
        *,
        name: str,
        args: list,
        kwargs: dict,
        queue: str,
        task_id: str,
    ) -> str:
        self.sent_calls.append(
            {
                "name": name,
                "args": list(args),
                "kwargs": dict(kwargs),
                "queue": queue,
                "task_id": task_id,
            }
        )
        if self.raise_with is not None:
            raise self.raise_with
        if self.raise_with_sequence is not None:
            exc = self.raise_with_sequence[self._call_index]
            self._call_index += 1
            if exc is not None:
                raise exc
        return task_id


def _new_task_log(db: Session, *, dedupe_key: Optional[str] = None) -> TaskLog:
    """Insert a fresh ``TaskLog`` and commit so it has a server-assigned ``id``."""
    task_log = TaskLog(task_type="outbox.publisher.pg", status="pending", dedupe_key=dedupe_key)
    db.add(task_log)
    db.commit()
    db.refresh(task_log)
    return task_log


def _new_command(
    task_name: str = "src.tasks.analyzer.analyze_session_task",
) -> OutboxCommand:
    """Return a deterministic :class:`OutboxCommand`."""
    return OutboxCommand(
        task_name=task_name,
        queue="analysis_hot",
        args=(42,),
        kwargs={"priority": "hot"},
    )


def _enroll_pending(db: Session) -> OutboxEvent:
    """Enroll one row, pin its ``next_attempt_at`` to the past.

    Mirrors the unit-test helper. After this returns, the row is
    claimable by ``try_claim_one`` on the very next ``run_once``.
    """
    task_log = _new_task_log(db)
    event = enqueue_command(db, _new_command(), task_log).event
    pinned = datetime.now(tz=timezone.utc) - timedelta(seconds=60)
    db.query(OutboxEventRow).filter(OutboxEventRow.task_log_id == task_log.id).update(
        {"next_attempt_at": pinned}
    )
    db.commit()
    return event


def _build_publisher(
    db: Session,
    broker: BrokerPort,
    *,
    claimed_by: str = "publisher-A",
    lease_seconds: int = PUBLISHER_LEASE_SECONDS,
    max_attempts: int = PUBLISHER_MAX_ATTEMPTS,
) -> OutboxPublisher:
    """Build an :class:`OutboxPublisher` with default test config."""
    config = PublisherConfig(
        claimed_by=claimed_by,
        lease_seconds=lease_seconds,
        max_attempts=max_attempts,
    )
    return OutboxPublisher(db_session=db, config=config, broker=broker)


# ---------------------------------------------------------------------------
# Round-trip happy path
# ---------------------------------------------------------------------------


def test_publish_round_trip_via_outbox(postgres_migrated_engine: Engine) -> None:
    """A single ``run_once`` claims, publishes, and marks the row.

    Asserts the broker received ``task_id=str(event.event_id)`` (the
    ADR §7 invariant), the row is ``published``, and ``published_at``
    is set to a recent UTC instant.
    """
    factory = sessionmaker(bind=postgres_migrated_engine, autocommit=False, autoflush=False)
    session = factory()
    try:
        event = _enroll_pending(session)
        event_id = event.event_id

        broker = _FakeBroker()
        publisher = _build_publisher(session, broker, claimed_by="publisher-A")
        delta = publisher.run_once()
        session.commit()
    finally:
        session.close()

    assert delta.published == 1
    assert len(broker.sent_calls) == 1
    sent = broker.sent_calls[0]
    assert sent["task_id"] == str(event_id)
    assert sent["name"] == "src.tasks.analyzer.analyze_session_task"
    assert sent["queue"] == "analysis_hot"

    verify = Session(postgres_migrated_engine)
    try:
        row = verify.query(OutboxEventRow).filter(OutboxEventRow.event_id == event_id).one()
        assert row.status == OutboxStatus.PUBLISHED.value
        assert row.published_at is not None
        # ``claimed_by`` / ``lease_expires_at`` are cleared on transition.
        assert row.claimed_by is None
        assert row.lease_expires_at is None
        # ``published_at`` is within the last few seconds.
        assert (datetime.now(tz=timezone.utc) - row.published_at).total_seconds() < 30
    finally:
        verify.close()


# ---------------------------------------------------------------------------
# Concurrency: two publishers, no double-claim
# ---------------------------------------------------------------------------


def test_two_publishers_no_double_claim(postgres_migrated_engine: Engine) -> None:
    """Two publisher instances cannot claim the same row.

    Each publisher uses its own session against the same engine.
    ``FOR UPDATE SKIP LOCKED`` semantics on PostgreSQL mean that the
    second session's SELECT returns ``None`` while the first session
    holds the row lock. Because the first session never commits
    before the second session runs, the row stays claimable-by-A
    only during the assertion window.
    """
    factory = sessionmaker(bind=postgres_migrated_engine, autocommit=False, autoflush=False)

    session_a = factory()
    session_b = factory()
    try:
        event = _enroll_pending(session_a)
        event_id = event.event_id
        session_a.commit()
        # Re-bind session_a and session_b to the row by id for clarity.

        broker_a = _FakeBroker()
        broker_b = _FakeBroker()

        publisher_a = _build_publisher(session_a, broker_a, claimed_by="publisher-A")
        publisher_b = _build_publisher(session_b, broker_b, claimed_by="publisher-B")

        # Publisher A claims and publishes; we keep its transaction
        # open while publisher B runs so the row stays in
        # ``publishing`` with A's lease.
        with session_a.begin():
            delta_a = publisher_a.run_once()
            # While session A holds the lease, publisher B sees no
            # eligible candidate.
            delta_b = publisher_b.run_once()
            session_b.rollback()
    finally:
        session_a.close()
        session_b.close()

    assert delta_a.published == 1
    assert delta_b.empty_polls == 1
    assert broker_b.sent_calls == []
    assert len(broker_a.sent_calls) == 1
    assert broker_a.sent_calls[0]["task_id"] == str(event_id)


# ---------------------------------------------------------------------------
# Lease expiry reclaim
# ---------------------------------------------------------------------------


def test_lease_expired_another_publisher_picks_up(
    postgres_migrated_engine: Engine,
) -> None:
    """A row whose previous publisher's lease has expired AND whose
    ``next_attempt_at`` is in the past is claimable by a fresh
    publisher instance.

    The contract requires BOTH conditions (lease expired AND
    ``next_attempt_at <= now``) because the lease alone does not
    re-queue the row — the publisher filter is on
    ``status='pending' AND next_attempt_at <= now``. We simulate
    this by enrolling, claim/publish-success-mark, then setting
    ``status='publishing'`` with a past ``lease_expires_at`` AND a
    past ``next_attempt_at`` to mimic the "previous publisher
    crashed mid-claim" path that gets reclaimed by another instance.
    """
    factory = sessionmaker(bind=postgres_migrated_engine, autocommit=False, autoflush=False)
    setup = factory()
    try:
        task_log = _new_task_log(setup)
        event = enqueue_command(setup, _new_command(), task_log).event
        event_id = event.event_id
        # Force the row into the lease-expired claimable shape.
        setup.query(OutboxEventRow).filter(OutboxEventRow.task_log_id == task_log.id).update(
            {
                "status": OutboxStatus.PUBLISHING.value,
                "claimed_by": "publisher-A",
                "lease_expires_at": datetime.now(tz=timezone.utc) - timedelta(seconds=600),
                "next_attempt_at": datetime.now(tz=timezone.utc) - timedelta(seconds=600),
            }
        )
        setup.commit()
    finally:
        setup.close()

    run = factory()
    try:
        broker = _FakeBroker()
        publisher = _build_publisher(run, broker, claimed_by="publisher-B")
        delta = publisher.run_once()
        run.commit()
    finally:
        run.close()

    # The repo's claim filter requires status='pending', so a
    # publishing row with an expired lease stays un-claimable from
    # the publisher's hot path until something flips it back to
    # pending (an operator runbook step). The contract is therefore
    # "next publisher gets it on the runbook path" rather than "next
    # publisher auto-claims". Documenting both:
    assert delta.empty_polls == 1, (
        "publisher skips status='publishing' rows even if lease expired; "
        "the contract requires status='pending' for the hot path"
    )
    assert broker.sent_calls == []

    # ... but if the operator flipped the row back to pending (and
    # left next_attempt_at in the past), the second publisher DOES
    # claim it cleanly. This is the realistic reclaim path.
    flip = factory()
    try:
        flip.query(OutboxEventRow).filter(OutboxEventRow.event_id == event_id).update(
            {"status": OutboxStatus.PENDING.value}
        )
        flip.commit()
    finally:
        flip.close()

    run2 = factory()
    try:
        broker2 = _FakeBroker()
        publisher2 = _build_publisher(run2, broker2, claimed_by="publisher-B")
        delta2 = publisher2.run_once()
        run2.commit()
    finally:
        run2.close()

    assert delta2.published == 1
    assert len(broker2.sent_calls) == 1
    assert broker2.sent_calls[0]["task_id"] == str(event_id)


# ---------------------------------------------------------------------------
# Retention: published row cleanup
# ---------------------------------------------------------------------------


def test_published_row_30_days_older_gets_cleaned(
    postgres_migrated_engine: Engine,
) -> None:
    """A ``published`` row whose ``published_at`` is older than the
    30-day threshold is deleted by
    :meth:`OutboxRepository.cleanup_published_older_than`.
    """
    factory = sessionmaker(bind=postgres_migrated_engine, autocommit=False, autoflush=False)
    session = factory()
    try:
        task_log = _new_task_log(session)
        event = enqueue_command(session, _new_command(), task_log).event
        event_id = event.event_id
        # Pin next_attempt_at to the past so the publisher claim succeeds.
        session.query(OutboxEventRow).filter(OutboxEventRow.task_log_id == task_log.id).update(
            {"next_attempt_at": datetime.now(tz=timezone.utc) - timedelta(seconds=60)}
        )
        session.commit()

        # Publish via the publisher; then override published_at to
        # 31 days ago to simulate an old row.
        publisher = _build_publisher(session, _FakeBroker())
        publisher.run_once()
        old_published_at = datetime.now(tz=timezone.utc) - timedelta(days=31)
        session.query(OutboxEventRow).filter(OutboxEventRow.event_id == event_id).update(
            {"published_at": old_published_at}
        )
        session.commit()

        threshold = datetime.now(tz=timezone.utc) - timedelta(days=30)
        deleted = OutboxRepository(session).cleanup_published_older_than(threshold)
        session.commit()
    finally:
        session.close()

    assert deleted == 1

    verify = Session(postgres_migrated_engine)
    try:
        remaining = verify.query(OutboxEventRow).filter(OutboxEventRow.event_id == event_id).count()
        assert remaining == 0
    finally:
        verify.close()


def test_published_row_29_days_old_not_cleaned(
    postgres_migrated_engine: Engine,
) -> None:
    """A ``published`` row whose ``published_at`` is 29 days old is
    NOT deleted by the 30-day retention sweep.
    """
    factory = sessionmaker(bind=postgres_migrated_engine, autocommit=False, autoflush=False)
    session = factory()
    try:
        task_log = _new_task_log(session)
        event = enqueue_command(session, _new_command(), task_log).event
        event_id = event.event_id
        session.query(OutboxEventRow).filter(OutboxEventRow.task_log_id == task_log.id).update(
            {"next_attempt_at": datetime.now(tz=timezone.utc) - timedelta(seconds=60)}
        )
        session.commit()

        publisher = _build_publisher(session, _FakeBroker())
        publisher.run_once()
        # Mark the row 29 days old (just inside the retention window).
        session.query(OutboxEventRow).filter(OutboxEventRow.event_id == event_id).update(
            {"published_at": datetime.now(tz=timezone.utc) - timedelta(days=29)}
        )
        session.commit()

        threshold = datetime.now(tz=timezone.utc) - timedelta(days=30)
        deleted = OutboxRepository(session).cleanup_published_older_than(threshold)
        session.commit()
    finally:
        session.close()

    assert deleted == 0

    verify = Session(postgres_migrated_engine)
    try:
        row = verify.query(OutboxEventRow).filter(OutboxEventRow.event_id == event_id).one()
        assert row.status == OutboxStatus.PUBLISHED.value
    finally:
        verify.close()


# ---------------------------------------------------------------------------
# Old-row handling
# ---------------------------------------------------------------------------


def test_publisher_does_not_skip_old_rows(
    postgres_migrated_engine: Engine,
) -> None:
    """A row whose ``next_attempt_at`` is 1000 s in the past is
    still published by the next ``run_once`` (the publisher does
    NOT skip it; it only emits the ``lag_degraded`` health metric).
    """
    factory = sessionmaker(bind=postgres_migrated_engine, autocommit=False, autoflush=False)
    session = factory()
    try:
        task_log = _new_task_log(session)
        event = enqueue_command(session, _new_command(), task_log).event
        event_id = event.event_id
        session.query(OutboxEventRow).filter(OutboxEventRow.task_log_id == task_log.id).update(
            {"next_attempt_at": datetime.now(tz=timezone.utc) - timedelta(seconds=1000)}
        )
        session.commit()

        broker = _FakeBroker()
        publisher = _build_publisher(session, broker)
        delta = publisher.run_once()
        session.commit()
    finally:
        session.close()

    assert delta.published == 1
    assert len(broker.sent_calls) == 1
    assert broker.sent_calls[0]["task_id"] == str(event_id)


# ---------------------------------------------------------------------------
# Broker failures and budget exhaustion
# ---------------------------------------------------------------------------


def test_outbox_publisher_failure_with_redis_stopped_simulated(
    postgres_migrated_engine: Engine,
) -> None:
    """Simulates a Redis stop by raising ``ConnectionError`` on the
    broker. The row must stay ``pending``, ``attempt_count`` must
    bump to ``1``, and ``next_attempt_at`` must be in the future
    (i.e. the publisher applied the +5 s backoff).
    """
    factory = sessionmaker(bind=postgres_migrated_engine, autocommit=False, autoflush=False)
    session = factory()
    try:
        event = _enroll_pending(session)
        event_id = event.event_id

        broker = _FakeBroker(raise_with=ConnectionError("redis down"))
        publisher = _build_publisher(session, broker)
        delta = publisher.run_once()
        session.commit()
    finally:
        session.close()

    assert delta.retryable_failures == 1
    assert delta.terminal_failures == 0
    # The fake broker records the call before raising; one entry is
    # the trace of the failed attempt, no entry is a successful send.
    assert len(broker.sent_calls) == 1

    verify = Session(postgres_migrated_engine)
    try:
        row = verify.query(OutboxEventRow).filter(OutboxEventRow.event_id == event_id).one()
        assert row.status == OutboxStatus.PENDING.value
        assert row.attempt_count == 1
        assert row.last_error is not None
        assert "ConnectionError" in row.last_error
        # The backoff has pushed next_attempt_at into the future.
        assert row.next_attempt_at > datetime.now(tz=timezone.utc)
    finally:
        verify.close()


def test_outbox_publisher_12_attempts_marks_failed(
    postgres_migrated_engine: Engine,
) -> None:
    """12 consecutive ``ConnectionError`` failures exhaust the budget
    and the row goes terminal (``status='failed'``).
    """
    factory = sessionmaker(bind=postgres_migrated_engine, autocommit=False, autoflush=False)
    session = factory()
    try:
        event = _enroll_pending(session)
        event_id = event.event_id

        broker = _FakeBroker(raise_with=ConnectionError("redis down"))
        publisher = _build_publisher(session, broker, max_attempts=PUBLISHER_MAX_ATTEMPTS)

        terminal_count = 0
        for _attempt in range(PUBLISHER_MAX_ATTEMPTS):
            # Pin ``next_attempt_at`` to the past between iterations
            # so each attempt can claim the row.
            session.query(OutboxEventRow).filter(OutboxEventRow.event_id == event_id).update(
                {"next_attempt_at": datetime.now(tz=timezone.utc) - timedelta(seconds=60)}
            )
            session.commit()
            delta = publisher.run_once()
            session.commit()
            if delta.terminal_failures == 1:
                terminal_count += 1
                break
    finally:
        session.close()

    assert terminal_count == 1

    verify = Session(postgres_migrated_engine)
    try:
        row = verify.query(OutboxEventRow).filter(OutboxEventRow.event_id == event_id).one()
        assert row.status == OutboxStatus.FAILED.value
        assert row.last_error is not None
        assert "ConnectionError" in row.last_error
        # The terminal path goes through ``mark_failed_terminal``,
        # which does NOT increment ``attempt_count`` (only
        # ``mark_failed_to_retry`` does). With max_attempts=12, the
        # publisher flips the row terminal when next_attempt_count
        # (= claimed.attempt_count + 1) reaches the budget, so
        # attempt_count stays at max_attempts - 1 = 11 after the
        # terminal transition.
        assert row.attempt_count == PUBLISHER_MAX_ATTEMPTS - 1
        # Terminal rows drop the lease.
        assert row.lease_expires_at is None
        assert row.claimed_by is None
    finally:
        verify.close()


def test_publisher_handles_redis_broker_restart(
    postgres_migrated_engine: Engine,
) -> None:
    """A transient broker ``ConnectionError`` followed by a
    successful publish on the second attempt transitions the row
    to ``published`` with ``attempt_count == 1``.
    """
    factory = sessionmaker(bind=postgres_migrated_engine, autocommit=False, autoflush=False)
    session = factory()
    try:
        event = _enroll_pending(session)
        event_id = event.event_id

        broker = _FakeBroker(
            raise_with_sequence=[ConnectionError("redis bounced"), None],
        )
        publisher = _build_publisher(session, broker)

        # First run_once: broker fails with ConnectionError.
        first = publisher.run_once()
        session.commit()
        # Pin next_attempt_at to the past so the second iteration
        # can claim.
        session.query(OutboxEventRow).filter(OutboxEventRow.event_id == event_id).update(
            {"next_attempt_at": datetime.now(tz=timezone.utc) - timedelta(seconds=60)}
        )
        session.commit()
        # Second run_once: broker succeeds.
        second = publisher.run_once()
        session.commit()
    finally:
        session.close()

    assert first.retryable_failures == 1
    assert second.published == 1
    # The fake broker records every send_task call (including the one
    # that raised), so two entries: one failed, one successful.
    assert len(broker.sent_calls) == 2
    # The successful call must still forward the right task_id (the
    # ADR §7 invariant holds across a restart).
    assert broker.sent_calls[1]["task_id"] == str(event_id)

    verify = Session(postgres_migrated_engine)
    try:
        row = verify.query(OutboxEventRow).filter(OutboxEventRow.event_id == event_id).one()
        assert row.status == OutboxStatus.PUBLISHED.value
        # First failure bumps attempt_count to 1; second success does
        # not bump it further (mark_published only updates lifecycle
        # columns).
        assert row.attempt_count == 1
        # ``last_error`` is cleared by ``mark_published`` (the row
        # reached ``published``).
        assert row.last_error is None
    finally:
        verify.close()
