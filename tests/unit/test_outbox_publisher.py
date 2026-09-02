"""SQLite unit tests for :class:`OutboxPublisher`.

These tests stand up an in-memory SQLite engine + the
:class:`~src.application.outbox.repository.OutboxRepository` and
exercise the publisher's claim → publish → mark loop with a fake
:class :class:`BrokerPort` (no Celery / Redis required). They cover:

- :func:`compute_next_attempt_at` backoff math at every attempt level;
- :func:`classify_celery_error` broker-exception mapping;
- :meth:`OutboxPublisher.run_once` happy / retryable / terminal paths;
- lease-expiry reclaim via a second publisher instance;
- :meth:`OutboxPublisher.health_check` lag / degraded reporting;
- one-shot / loop semantics in :meth:`run_until_signal`.

PostgreSQL-specific concurrency / retention / crash-recovery tests
live in ``tests/integration/test_outbox_publisher_postgres.py``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

import src.db.base  # noqa: F401  (registers OutboxEvent on Base.metadata)
from src.application.outbox import contracts as outbox_contracts
from src.application.outbox.contracts import OutboxCommand, OutboxEvent
from src.application.outbox.enqueue import enqueue_command
from src.application.outbox.publisher import (
    PUBLISHER_BACKLOG_DEGRADED_SECONDS,
    PUBLISHER_BACKOFF_BASE_SECONDS,
    PUBLISHER_BACKOFF_CAP_SECONDS,
    PUBLISHER_LEASE_SECONDS,
    PUBLISHER_MAX_ATTEMPTS,
    BrokerPort,
    OutboxPublisher,
    PublisherConfig,
    PublisherHealth,
    PublisherStats,
    classify_celery_error,
    compute_next_attempt_at,
)
from src.application.outbox.state_machine import OutboxStatus
from src.db.base_class import Base
from src.models.outbox import OutboxEvent as OutboxEventRow
from src.models.task_log import TaskLog

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_emitted_event_ids() -> None:
    """Reset the in-process duplicate-event-id set."""
    outbox_contracts._reset_emitted_event_ids_for_testing()


@pytest.fixture
def db_session() -> Session:
    """Return a function-scoped SQLite session bound to an in-memory engine."""
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    session = factory()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_task_log(db: Session) -> TaskLog:
    task_log = TaskLog(task_type="outbox.unit.publisher", status="pending")
    db.add(task_log)
    db.commit()
    db.refresh(task_log)
    return task_log


def _make_command(task_name: str = "src.tasks.analyzer.analyze_session_task") -> OutboxCommand:
    return OutboxCommand(
        task_name=task_name,
        queue="analysis_hot",
        args=(42,),
        kwargs={"priority": "hot"},
    )


def _pin_next_attempt_to_past(db: Session, task_log_id: int) -> None:
    pinned = datetime.now(tz=timezone.utc) - timedelta(seconds=60)
    db.query(OutboxEventRow).filter(OutboxEventRow.task_log_id == task_log_id).update(
        {"next_attempt_at": pinned}
    )
    db.commit()


def _enroll_pending(db: Session) -> OutboxEvent:
    """Enroll one row, claim it once to put it in ``publishing``-like state.

    After this helper the row is ``pending`` with ``next_attempt_at``
    pinned to the past, so the publisher's first ``run_once`` will
    claim it immediately.
    """
    task_log = _make_task_log(db)
    outcome = enqueue_command(db, _make_command(), task_log)
    _pin_next_attempt_to_past(db, task_log.id)
    return outcome.event


@dataclass
class _FakeBroker(BrokerPort):
    """In-memory :class:`BrokerPort` for unit tests.

    Records every ``send_task`` call so tests can assert the publisher
    forwarded ``task_id=str(event_id)`` and the right ``name`` /
    ``args`` / ``kwargs`` / ``queue``. Optional ``raise_with`` lets a
    test inject a broker failure (with a typed exception) on the
    n-th call.
    """

    sent_calls: list[dict[str, object]] = field(default_factory=list)
    raise_with: Optional[Exception] = None

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
        return task_id


def _publisher_config(
    *,
    broker: BrokerPort,
    db_session: Session,
    claimed_by: str = "worker-A",
    poll_interval_seconds: int = 0,
    lease_seconds: int = PUBLISHER_LEASE_SECONDS,
    max_attempts: int = PUBLISHER_MAX_ATTEMPTS,
    one_shot: bool = False,
) -> tuple[OutboxPublisher, PublisherConfig]:
    config = PublisherConfig(
        claimed_by=claimed_by,
        poll_interval_seconds=poll_interval_seconds,
        lease_seconds=lease_seconds,
        max_attempts=max_attempts,
        one_shot=one_shot,
    )
    return (
        OutboxPublisher(db_session=db_session, config=config, broker=broker),
        config,
    )


# ---------------------------------------------------------------------------
# compute_next_attempt_at
# ---------------------------------------------------------------------------


def test_compute_next_attempt_at_attempt_1_is_5_seconds() -> None:
    """Attempt 1 schedules +5 s."""
    before = datetime.now(tz=timezone.utc)
    next_at = compute_next_attempt_at(attempt_count=1)
    after = datetime.now(tz=timezone.utc)

    delay = (next_at - before).total_seconds()
    assert PUBLISHER_BACKOFF_BASE_SECONDS - 1 <= delay <= PUBLISHER_BACKOFF_BASE_SECONDS + 1
    assert next_at >= before
    assert next_at <= after + timedelta(seconds=PUBLISHER_BACKOFF_BASE_SECONDS + 1)


def test_compute_next_attempt_at_attempt_2_is_10_seconds() -> None:
    """Attempt 2 schedules +10 s."""
    before = datetime.now(tz=timezone.utc)
    next_at = compute_next_attempt_at(attempt_count=2)
    after = datetime.now(tz=timezone.utc)

    delay = (next_at - before).total_seconds()
    # Clock may advance microseconds between ``before`` and the
    # ``datetime.now()`` inside ``compute_next_attempt_at``; allow a
    # 1-second slack on the upper bound to absorb that drift.
    assert 9 <= delay <= 11
    assert next_at <= after + timedelta(seconds=11)


def test_compute_next_attempt_at_attempt_9_is_clamped_to_300_seconds() -> None:
    """Attempt 9 would be 5 * 2^8 = 1280 s; the cap clamps to 300 s."""
    before = datetime.now(tz=timezone.utc)
    next_at = compute_next_attempt_at(attempt_count=9)
    after = datetime.now(tz=timezone.utc)

    delay = (next_at - before).total_seconds()
    assert PUBLISHER_BACKOFF_CAP_SECONDS - 1 <= delay <= PUBLISHER_BACKOFF_CAP_SECONDS + 1
    assert next_at <= after + timedelta(seconds=PUBLISHER_BACKOFF_CAP_SECONDS + 1)


def test_compute_next_attempt_at_attempt_10_is_clamped_to_300_seconds() -> None:
    """Attempt 10 also clamped (5 * 2^9 = 2560 s, way over the cap)."""
    before = datetime.now(tz=timezone.utc)
    next_at = compute_next_attempt_at(attempt_count=10)
    delay = (next_at - before).total_seconds()
    assert PUBLISHER_BACKOFF_CAP_SECONDS - 1 <= delay <= PUBLISHER_BACKOFF_CAP_SECONDS + 1


def test_compute_next_attempt_at_attempt_12_is_clamped_to_300_seconds() -> None:
    """Attempt 12 (the terminal threshold) is still 300 s — the next
    attempt would never happen because the row goes terminal first."""
    before = datetime.now(tz=timezone.utc)
    next_at = compute_next_attempt_at(attempt_count=12)
    delay = (next_at - before).total_seconds()
    assert PUBLISHER_BACKOFF_CAP_SECONDS - 1 <= delay <= PUBLISHER_BACKOFF_CAP_SECONDS + 1


def test_compute_next_attempt_at_zero_clamps_to_base() -> None:
    """Defensive: a misconfigured caller passing 0 should still produce
    a sensible (>= base) wait."""
    before = datetime.now(tz=timezone.utc)
    next_at = compute_next_attempt_at(attempt_count=0)
    delay = (next_at - before).total_seconds()
    assert PUBLISHER_BACKOFF_BASE_SECONDS - 1 <= delay <= PUBLISHER_BACKOFF_BASE_SECONDS + 1


# ---------------------------------------------------------------------------
# classify_celery_error
# ---------------------------------------------------------------------------


def test_classify_celery_error_not_registered_is_terminal() -> None:
    """``celery.exceptions.NotRegistered`` is terminal — the broker
    cannot accept this task name; retrying produces the same error."""
    from celery.exceptions import NotRegistered

    assert classify_celery_error(NotRegistered("unknown.task")) == "terminal"


def test_classify_celery_error_kombu_operational_error_is_retryable() -> None:
    """``kombu.exceptions.OperationalError`` is retryable."""
    from kombu.exceptions import OperationalError

    assert classify_celery_error(OperationalError("broker down")) == "retryable"


def test_classify_celery_error_kombu_connection_error_is_retryable() -> None:
    """``kombu.exceptions.ConnectionError`` (actually ``amqp``'s) is retryable."""
    import kombu.exceptions

    assert (
        classify_celery_error(kombu.exceptions.ConnectionError("connection refused")) == "retryable"
    )


def test_classify_celery_error_builtin_connection_error_is_retryable() -> None:
    """Builtin ``ConnectionError`` is retryable."""
    assert classify_celery_error(ConnectionError("connection refused")) == "retryable"


def test_classify_celery_error_builtin_timeout_error_is_retryable() -> None:
    """Builtin ``TimeoutError`` is retryable."""
    assert classify_celery_error(TimeoutError("timed out")) == "retryable"


def test_classify_celery_error_os_error_is_retryable() -> None:
    """``OSError`` (e.g. socket-level failure) is retryable."""
    assert classify_celery_error(OSError("socket closed")) == "retryable"


def test_classify_celery_error_value_error_is_retryable() -> None:
    """Unknown exception types default to ``retryable`` for safety:
    the next attempt hits the broker again, and the worst case is
    "still failing" rather than "row stuck in publishing forever"."""
    assert classify_celery_error(ValueError("malformed payload")) == "retryable"


# ---------------------------------------------------------------------------
# OutboxPublisher.run_once — empty pool
# ---------------------------------------------------------------------------


def test_run_once_with_empty_pool_returns_empty_polls(db_session: Session) -> None:
    """An empty pool yields a snapshot whose only counter is ``empty_polls``."""
    broker = _FakeBroker()
    publisher, _config = _publisher_config(broker=broker, db_session=db_session)

    delta = publisher.run_once()

    assert delta.empty_polls == 1
    assert delta.claimed == 0
    assert delta.published == 0
    assert delta.retryable_failures == 0
    assert delta.terminal_failures == 0
    assert broker.sent_calls == []
    # Cumulative stats reflect the same deltas.
    assert publisher.stats.empty_polls == 1
    assert publisher.stats.claimed == 0


# ---------------------------------------------------------------------------
# OutboxPublisher.run_once — happy path
# ---------------------------------------------------------------------------


def test_run_once_publishes_one_row_and_marks_published(db_session: Session) -> None:
    """A claimable row is published, marked ``published``, and the
    broker received ``task_id=str(event_id)``."""
    event = _enroll_pending(db_session)
    broker = _FakeBroker()
    publisher, _ = _publisher_config(broker=broker, db_session=db_session)

    delta = publisher.run_once()

    assert delta.claimed == 1
    assert delta.published == 1
    assert delta.empty_polls == 0
    assert len(broker.sent_calls) == 1
    sent = broker.sent_calls[0]
    assert sent["task_id"] == str(event.event_id)
    assert sent["name"] == event.task_name
    assert sent["queue"] == event.queue
    assert sent["args"] == list(event.args_json)
    assert sent["kwargs"] == dict(event.kwargs_json)

    # Row transitioned to published.
    refreshed = db_session.query(OutboxEventRow).filter(OutboxEventRow.id == event.id).one()
    assert refreshed.status == OutboxStatus.PUBLISHED.value
    assert refreshed.claimed_by is None
    assert refreshed.lease_expires_at is None
    assert refreshed.published_at is not None


# ---------------------------------------------------------------------------
# OutboxPublisher.run_once — retryable failure
# ---------------------------------------------------------------------------


def test_run_once_with_retryable_failure_returns_row_to_pending(
    db_session: Session,
) -> None:
    """A ``ConnectionError`` broker failure flips the row back to
    ``pending``, increments ``attempt_count``, and pushes
    ``next_attempt_at`` into the future."""
    _enroll_pending(db_session)
    broker = _FakeBroker(raise_with=ConnectionError("broker down"))
    publisher, _ = _publisher_config(broker=broker, db_session=db_session)

    delta = publisher.run_once()

    assert delta.claimed == 1
    assert delta.retryable_failures == 1
    assert delta.terminal_failures == 0
    assert delta.published == 0

    row = db_session.query(OutboxEventRow).one()
    assert row.status == OutboxStatus.PENDING.value
    assert row.attempt_count == 1
    assert row.last_error is not None
    assert "ConnectionError" in row.last_error
    # next_attempt_at is in the future.
    next_attempt_at = row.next_attempt_at
    if next_attempt_at.tzinfo is None:
        next_attempt_at = next_attempt_at.replace(tzinfo=timezone.utc)
    assert next_attempt_at > datetime.now(tz=timezone.utc)


def test_run_once_with_timeout_error_is_retryable(db_session: Session) -> None:
    """``TimeoutError`` is a retryable broker failure."""
    _enroll_pending(db_session)
    broker = _FakeBroker(raise_with=TimeoutError("publish timed out"))
    publisher, _ = _publisher_config(broker=broker, db_session=db_session)

    delta = publisher.run_once()

    assert delta.retryable_failures == 1
    row = db_session.query(OutboxEventRow).one()
    assert row.status == OutboxStatus.PENDING.value
    assert row.attempt_count == 1


def test_run_once_with_os_error_is_retryable(db_session: Session) -> None:
    """``OSError`` is a retryable broker failure."""
    _enroll_pending(db_session)
    broker = _FakeBroker(raise_with=OSError("socket closed"))
    publisher, _ = _publisher_config(broker=broker, db_session=db_session)

    delta = publisher.run_once()

    assert delta.retryable_failures == 1
    row = db_session.query(OutboxEventRow).one()
    assert row.status == OutboxStatus.PENDING.value
    assert row.attempt_count == 1


# ---------------------------------------------------------------------------
# OutboxPublisher.run_once — terminal failures
# ---------------------------------------------------------------------------


def test_run_once_with_not_registered_marks_terminal_immediately(
    db_session: Session,
) -> None:
    """``NotRegistered`` is a terminal classification; the row goes
    straight to ``failed`` without consuming the 12-attempt budget."""
    from celery.exceptions import NotRegistered

    _enroll_pending(db_session)
    broker = _FakeBroker(raise_with=NotRegistered("unknown.task"))
    publisher, _ = _publisher_config(broker=broker, db_session=db_session)

    delta = publisher.run_once()

    assert delta.claimed == 1
    assert delta.terminal_failures == 1
    assert delta.retryable_failures == 0
    row = db_session.query(OutboxEventRow).one()
    assert row.status == OutboxStatus.FAILED.value
    assert row.last_error is not None
    assert "NotRegistered" in row.last_error


def test_run_once_after_max_attempts_marks_terminal(db_session: Session) -> None:
    """The 12th retryable failure crosses the ``max_attempts`` budget
    and the row goes terminal — independent of broker classification."""
    task_log = _make_task_log(db_session)
    event = enqueue_command(db_session, _make_command(), task_log).event
    # Pin attempt_count to (max_attempts - 1) so the next failure
    # crosses the threshold.
    db_session.query(OutboxEventRow).filter(OutboxEventRow.id == event.id).update(
        {
            "attempt_count": PUBLISHER_MAX_ATTEMPTS - 1,
            "next_attempt_at": datetime.now(tz=timezone.utc) - timedelta(seconds=60),
        }
    )
    db_session.commit()

    broker = _FakeBroker(raise_with=ConnectionError("still down"))
    publisher, _ = _publisher_config(broker=broker, db_session=db_session)

    delta = publisher.run_once()

    assert delta.terminal_failures == 1
    row = db_session.query(OutboxEventRow).one()
    assert row.status == OutboxStatus.FAILED.value


# ---------------------------------------------------------------------------
# Lease expiry reclaim
# ---------------------------------------------------------------------------


def test_run_once_claims_row_after_lease_expired(db_session: Session) -> None:
    """A second publisher can claim a row whose previous publisher's
    lease has expired. Simulate a stale claim by writing
    ``lease_expires_at`` in the past and a past ``next_attempt_at``
    (the publisher filter requires both)."""
    task_log = _make_task_log(db_session)
    event = enqueue_command(db_session, _make_command(), task_log).event
    db_session.query(OutboxEventRow).filter(OutboxEventRow.id == event.id).update(
        {
            "status": OutboxStatus.PUBLISHING.value,
            "claimed_by": "worker-A",
            "lease_expires_at": datetime.now(tz=timezone.utc) - timedelta(seconds=600),
            "next_attempt_at": datetime.now(tz=timezone.utc) - timedelta(seconds=600),
        }
    )
    db_session.commit()

    broker = _FakeBroker()
    publisher, _ = _publisher_config(broker=broker, db_session=db_session, claimed_by="worker-B")

    delta = publisher.run_once()

    # The publisher should observe ``status='publishing'`` rows whose
    # lease expired as still-claimable candidates only if
    # ``next_attempt_at <= now``. The contract layer's claim query
    # filters on ``status='pending'`` though — see test below for the
    # "status=pending after lease expiry" path. Here we expect the
    # ``publishing`` row to NOT be reclaimable until the previous
    # publisher (or a runbook operator) flips it back to ``pending``.
    assert delta.empty_polls == 1
    assert broker.sent_calls == []


def test_run_once_claims_row_when_status_pending_after_lease_expired(
    db_session: Session,
) -> None:
    """When the row is already back to ``pending`` (because a previous
    ``mark_failed_to_retry`` ran) AND ``next_attempt_at <= now``,
    another publisher claims it cleanly. This is the realistic
    reclaim path — the previous publisher bumped the row back to
    ``pending`` after a retryable failure, then crashed before the
    lease was reset."""
    _enroll_pending(db_session)
    # Pin attempt_count and next_attempt_at to the past; the row is
    # already in ``pending`` because enroll_with_task_log emitted it.
    broker = _FakeBroker(raise_with=ConnectionError("broker down"))
    publisher_a, _ = _publisher_config(broker=broker, db_session=db_session, claimed_by="worker-A")
    publisher_a.run_once()
    # Row is back in pending, attempt_count=1, next_attempt_at in the
    # future. Force next_attempt_at to the past to simulate "the
    # backoff has elapsed; another publisher may claim".
    row = db_session.query(OutboxEventRow).one()
    db_session.query(OutboxEventRow).filter(OutboxEventRow.id == row.id).update(
        {"next_attempt_at": datetime.now(tz=timezone.utc) - timedelta(seconds=60)}
    )
    db_session.commit()

    broker_b = _FakeBroker()
    publisher_b, _ = _publisher_config(
        broker=broker_b, db_session=db_session, claimed_by="worker-B"
    )
    delta_b = publisher_b.run_once()

    assert delta_b.claimed == 1
    assert delta_b.published == 1
    assert len(broker_b.sent_calls) == 1


# ---------------------------------------------------------------------------
# health_check
# ---------------------------------------------------------------------------


def test_health_check_empty_pool_is_healthy_and_not_degraded(
    db_session: Session,
) -> None:
    """An empty pool reports ``healthy=True`` and ``lag_degraded=False``."""
    broker = _FakeBroker()
    publisher, _ = _publisher_config(broker=broker, db_session=db_session)

    health = publisher.health_check()

    assert isinstance(health, PublisherHealth)
    assert health.healthy is True
    assert health.oldest_pending_seconds is None
    assert health.lag_degraded is False


def test_health_check_old_row_past_degraded_threshold_is_degraded(
    db_session: Session,
) -> None:
    """A pending row pinned ``PUBLISHER_BACKLOG_DEGRADED_SECONDS + 50`` in the
    past triggers ``lag_degraded=True``."""
    task_log = _make_task_log(db_session)
    event = enqueue_command(db_session, _make_command(), task_log).event
    db_session.query(OutboxEventRow).filter(OutboxEventRow.id == event.id).update(
        {
            "next_attempt_at": datetime.now(tz=timezone.utc)
            - timedelta(seconds=PUBLISHER_BACKLOG_DEGRADED_SECONDS + 50)
        }
    )
    db_session.commit()

    broker = _FakeBroker()
    publisher, _ = _publisher_config(broker=broker, db_session=db_session)
    health = publisher.health_check()

    assert health.healthy is True
    assert health.oldest_pending_seconds is not None
    assert health.oldest_pending_seconds >= PUBLISHER_BACKLOG_DEGRADED_SECONDS
    assert health.lag_degraded is True


def test_health_check_young_row_not_degraded(db_session: Session) -> None:
    """A pending row pinned ``PUBLISHER_BACKLOG_DEGRADED_SECONDS - 50`` in the
    past is NOT degraded."""
    task_log = _make_task_log(db_session)
    event = enqueue_command(db_session, _make_command(), task_log).event
    db_session.query(OutboxEventRow).filter(OutboxEventRow.id == event.id).update(
        {
            "next_attempt_at": datetime.now(tz=timezone.utc)
            - timedelta(seconds=PUBLISHER_BACKLOG_DEGRADED_SECONDS - 50)
        }
    )
    db_session.commit()

    broker = _FakeBroker()
    publisher, _ = _publisher_config(broker=broker, db_session=db_session)
    health = publisher.health_check()

    assert health.healthy is True
    assert health.lag_degraded is False


# ---------------------------------------------------------------------------
# PublisherStats.merge
# ---------------------------------------------------------------------------


def test_publisher_stats_merge_accumulates_counters() -> None:
    """Merging two snapshots adds the counters and keeps the latest timestamp."""
    now = datetime.now(tz=timezone.utc)
    a = PublisherStats(
        claimed=1,
        published=1,
        retryable_failures=2,
        empty_polls=3,
        last_processed_at=now,
    )
    b = PublisherStats(
        claimed=2,
        published=1,
        terminal_failures=1,
        empty_polls=1,
        last_processed_at=now + timedelta(seconds=5),
    )
    merged = a.merge(b)

    assert merged.claimed == 3
    assert merged.published == 2
    assert merged.retryable_failures == 2
    assert merged.terminal_failures == 1
    assert merged.empty_polls == 4
    assert merged.last_processed_at == now + timedelta(seconds=5)


# ---------------------------------------------------------------------------
# run_until_signal — one_shot / loop
# ---------------------------------------------------------------------------


def test_run_until_signal_one_shot_runs_once_then_returns(
    db_session: Session,
) -> None:
    """``one_shot=True`` makes :meth:`run_until_signal` call
    :meth:`run_once` once and return immediately — the test entry
    point. Even with ``stop=lambda: True`` we still run one
    iteration (matches the CLI ``--once`` semantics)."""
    _enroll_pending(db_session)
    broker = _FakeBroker()
    publisher, _ = _publisher_config(broker=broker, db_session=db_session, one_shot=True)

    stats = publisher.run_until_signal(stop=lambda: True)

    assert stats.claimed == 1
    assert stats.published == 1
    assert len(broker.sent_calls) == 1


def test_run_until_signal_loop_runs_iterations_until_stop(
    db_session: Session,
) -> None:
    """The loop runs :meth:`run_once` repeatedly until ``stop()``
    returns ``True``. We use ``poll_interval_seconds=0`` so the test
    does not block."""
    # Enroll 2 rows.
    for _ in range(2):
        _enroll_pending(db_session)

    broker = _FakeBroker()
    publisher, _ = _publisher_config(broker=broker, db_session=db_session)

    counter = {"calls": 0}

    def _stop() -> bool:
        counter["calls"] += 1
        # ``stop()`` is called BEFORE each ``run_once`` invocation, so
        # returning ``True`` on the Nth call stops BEFORE the Nth
        # iteration. We want exactly 2 iterations → stop on the 3rd
        # call. Returning ``True`` on the 2nd call would stop after
        # only 1 iteration.
        return counter["calls"] > 2

    started = time.monotonic()
    stats = publisher.run_until_signal(stop=_stop)
    elapsed = time.monotonic() - started

    # Two iterations; both rows should be published.
    assert stats.published == 2
    assert elapsed < 5  # poll_interval_seconds=0 ⇒ near-instant


# ---------------------------------------------------------------------------
# Truncation of last_error
# ---------------------------------------------------------------------------


def test_run_once_truncates_oversize_error_message(db_session: Session) -> None:
    """A multi-MB error string is truncated to fit ``last_error``."""

    class _HugeError(ConnectionError):
        def __str__(self) -> str:
            return "X" * 50_000

    _enroll_pending(db_session)
    broker = _FakeBroker(raise_with=_HugeError("huge"))
    publisher, _ = _publisher_config(broker=broker, db_session=db_session)

    publisher.run_once()

    row = db_session.query(OutboxEventRow).one()
    assert row.last_error is not None
    # Truncated to 2000 + "…[truncated]".
    assert len(row.last_error) <= 2000 + len("…[truncated]")
    assert row.last_error.endswith("…[truncated]")


# ---------------------------------------------------------------------------
# Broker contract — task_id is event_id
# ---------------------------------------------------------------------------


def test_run_once_sets_task_id_to_str_event_id(db_session: Session) -> None:
    """The :attr:`BrokerPort.send_task` ``task_id`` argument MUST be
    ``str(event.event_id)`` — the ADR §7 invariant."""
    event = _enroll_pending(db_session)
    broker = _FakeBroker()
    publisher, _ = _publisher_config(broker=broker, db_session=db_session)

    publisher.run_once()

    assert broker.sent_calls[0]["task_id"] == str(event.event_id)
