"""Outbox publisher — claim → publish → mark the durable outbox rows.

This module is the **publisher** half of the PostgreSQL transactional
outbox defined in ADR ``docs/adr/0011-transactional-outbox-and-task-lifecycle.md``
(Wave 3, Todo 13). The contract layer (Todo 11) froze the field list,
the state machine, the retry parameters and the failure matrix. The
persistence layer (Todo 12) shipped the SQLAlchemy model, the Alembic
migration and the :class:`~src.application.outbox.repository.OutboxRepository`
that exposes ``try_claim_one`` / ``mark_published`` /
``mark_failed_to_retry`` / ``mark_failed_terminal`` /
``cleanup_published_older_than`` / ``count_pending_older_than``. This
module turns those seams into a **standalone, broker-aware Python
process** that owns the broker-side of the dual write.

Design constraints (ADR §7 / §8 + plan §11 "Outbox 默认参数")
============================================================

1. **Independent of Celery Beat.** The publisher is *not* a Celery task
   or a Celery beat entry. It is a standalone Python process / container
   that talks to PostgreSQL and to the Celery broker (Redis). Heartbeat
   / Celery beat failures do not stop publishes (ADR "Positive").
2. **``task_id`` is ``event_id``** — :meth:`BrokerPort.send_task` is
   always called with ``task_id=str(event.event_id)``. The consumer-side
   :func:`bind_or_create_running_task_log` uses the same ``event_id``
   for idempotency (ADR §7).
3. **Concurrency safety** — :meth:`OutboxRepository.try_claim_one` uses
   ``FOR UPDATE SKIP LOCKED`` on PostgreSQL so two publisher instances
   never contend on the same row. The SQLite unit-test path falls back
   to ``SELECT`` + conditional ``UPDATE`` under a writer-serialized
   connection (Todo 12 repository §"Concurrency notes").
4. **Crash recovery** — every claim sets
   ``lease_expires_at = now() + lease_seconds``. A publisher that
   crashes mid-claim leaves the row in ``publishing`` until the lease
   expires; another publisher then claims it (ADR §8 "Publisher crashes
   mid-claim"). The lease is the seam that makes "publish, then crash
   before mark_published" recoverable: the second publish hits the
   consumer-side ``bind_or_create_running_task_log`` stale-message
   short-circuit.
5. **At-least-once semantics** — duplicate publishes are possible
   (the row might get published twice if the lease expires between
   ``send_task`` and ``mark_published``); the consumer is responsible
   for idempotency (ADR §7). The publisher does *not* introduce
   exactly-once.
6. **Failure classification.** :func:`classify_celery_error` translates
   a broker exception to one of ``retryable`` / ``terminal``:

   - ``celery.exceptions.NotRegistered`` → ``terminal`` (the broker
     cannot accept this task name; retrying just produces the same
     error). The row goes straight to ``failed`` without consuming the
     12-attempt budget (ADR §8 "Broker rejects").
   - ``kombu.exceptions.OperationalError``,
     ``amqp.exceptions.ConnectionError``,
     ``ConnectionError``, ``TimeoutError``, ``OSError`` →
     ``retryable`` (the broker is unreachable; a later retry may
     succeed). Increments ``attempt_count`` and pushes the row back to
     ``pending`` with the next backoff.
   - Everything else → ``retryable`` for safety: the next attempt
     will hit the broker again, and the worst case is "still
     failing" rather than "row stuck in publishing forever". Operators
     see the exception in :attr:`OutboxEvent.last_error`.
7. **Lease expiry → reclaim.** ``try_claim_one`` is filtered by
   ``next_attempt_at <= now()``. ``next_attempt_at`` is bumped on every
   retry, so a row in ``publishing`` whose lease has expired *and*
   whose ``next_attempt_at`` is still in the future is **not**
   re-claimed by the same publisher loop on the next tick. The lease
   expiry path is exercised via the **next** claim attempt, which
   happens only after the publisher's lease has been forcibly reset
   (operations runbook, Todo 24) or the row has been ``mark_failed_to_retry``
   requeued with a fresh ``next_attempt_at``. This is the "lease
   expires → second publisher re-claims" path from ADR §8.

Failure matrix (mirrors ADR §8)
==============================

- Redis broker unreachable — ``send_task`` raises ``OperationalError``
  or ``ConnectionError``: ``classify_celery_error`` returns
  ``retryable``; ``mark_failed_to_retry`` with next backoff; row
  stays claimable.
- Publisher crashes mid-claim — lease not refreshed within
  ``lease_seconds``: another publisher's ``try_claim_one`` returns
  the row; same publish path.
- Publish, then crash before ``mark_published`` — lease expires:
  another publisher re-publishes; consumer's
  ``bind_or_create_running_task_log`` short-circuits on terminal
  TaskLog.
- Broker rejects (``NotRegistered``) — ``send_task`` raises
  ``NotRegistered``: ``classify_celery_error`` returns ``terminal``;
  ``mark_failed_terminal``; row is operationally terminal.
- ``attempt_count + 1 >= max_attempts`` — publisher increments
  ``attempt_count`` then compares: ``mark_failed_terminal``; row is
  operationally terminal.
- Two publishers contend — ``FOR UPDATE SKIP LOCKED``: one claim
  wins; the other returns ``None`` from ``try_claim_one``.
- Backlog > 300 s — :meth:`OutboxRepository.count_pending_older_than`:
  publisher does **not** skip / delete; supervisor emits the degraded
  metric.

CLI entry (``cli.py``) and docker-compose service
================================================

This module is consumable in three ways:

1. As an embedded library by another process — instantiate
   :class:`OutboxPublisher` directly and call :meth:`run_once` /
   :meth:`run_until_signal`.
2. As a unit-test fixture — same shape, with a fake ``BrokerPort`` and
   a controlled clock.
3. As a standalone CLI via :mod:`src.application.outbox.cli` (the
   ``outbox_publisher`` docker-compose service). The CLI owns the
   ``SessionLocal`` lifecycle and respects SIGTERM / SIGINT.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional, Protocol, Union

from sqlalchemy.orm import Session

from src.application.outbox.contracts import OutboxEvent
from src.application.outbox.repository import OutboxRepository

# ---------------------------------------------------------------------------
# Constants — mirror ADR §4 ("Retry parameters"). DO NOT change without an
# ADR amendment. The supervisor / operations runbook (Todo 24) reads the
# same numbers.
# ---------------------------------------------------------------------------

PUBLISHER_POLL_INTERVAL_SECONDS: int = 5
PUBLISHER_BATCH_SIZE: int = 100
PUBLISHER_LEASE_SECONDS: int = 60
PUBLISHER_BACKOFF_BASE_SECONDS: int = 5
PUBLISHER_BACKOFF_CAP_SECONDS: int = 300
PUBLISHER_MAX_ATTEMPTS: int = 12
PUBLISHER_PUBLISHED_RETENTION_DAYS: int = 30
PUBLISHER_BACKLOG_DEGRADED_SECONDS: int = 300

# Truncate the broker-side error so the DB column (TEXT, 2000 chars in the
# ADR contract) does not overflow. ``last_error`` is operator-facing,
# not for log analysis; full tracebacks should still land in the
# publisher's structured log (Todo 24).
PUBLISHER_ERROR_TRUNCATE_CHARS: int = 2000

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Backoff math
# ---------------------------------------------------------------------------


def compute_next_attempt_at(attempt_count: int) -> datetime:
    """Return the next ``next_attempt_at`` after a retryable failure.

    Implements ADR §4: ``min(backoff_cap, backoff_base * 2 ** (attempt_count - 1))``
    starting at ``attempt_count=1`` (so attempt 1 schedules +5 s, attempt 2
    +10 s, …, attempt 9 +2560 s clamped to +300 s, attempts 10-12 stay at
    +300 s).

    Args:
        attempt_count: The **next** attempt number — i.e. the value of
            ``OutboxEvent.attempt_count`` after this failure has been
            applied. The repository's :meth:`mark_failed_to_retry`
            performs ``attempt_count + 1`` and stores the result; this
            helper receives that post-increment value.

    Returns:
        The wall-clock instant the publisher should next try this row.
        Always timezone-aware UTC.
    """
    if attempt_count < 1:
        # Defensive: a misconfigured caller that passes ``0`` should not
        # produce a sub-millisecond retry. Clamp to the base so the
        # math is monotonically increasing.
        attempt_count = 1
    delay_seconds = PUBLISHER_BACKOFF_BASE_SECONDS * (2 ** (attempt_count - 1))
    delay_seconds = min(delay_seconds, PUBLISHER_BACKOFF_CAP_SECONDS)
    return datetime.now(tz=timezone.utc) + timedelta(seconds=delay_seconds)


# ---------------------------------------------------------------------------
# Broker port
# ---------------------------------------------------------------------------


class BrokerPort(Protocol):
    """Adapter port that publishes one Celery message.

    The publisher depends on this Protocol — never on the Celery app
    directly — so unit tests can pass a fake that records ``send_task``
    calls without standing up a Redis broker.

    Implementations MUST:

    - call ``celery_app.send_task(name, args=args, kwargs=kwargs,
      queue=queue, task_id=task_id)`` (or equivalent);
    - set ``task_id`` to the caller-supplied value (the
      :class:`OutboxEvent.event_id` UUID rendered as a string). The
      ``event_id`` ↔ ``task_id`` invariant is load-bearing (ADR §7);
    - raise the underlying broker exception verbatim so
      :func:`classify_celery_error` can inspect it;
    - treat any non-exception return as a successful broker round-trip.
      ``AsyncResult.id`` is the canonical return shape; Celery uses
      ``AsyncResult`` internally.
    """

    def send_task(
        self,
        *,
        name: str,
        args: list,
        kwargs: dict,
        queue: str,
        task_id: str,
    ) -> str:
        """Push one Celery message. Returns ``task_id`` (echoed)."""
        ...


class CeleryBrokerPort:
    """Production :class:`BrokerPort` that delegates to ``celery_app.send_task``.

    The Celery import is deferred to ``__init__`` so unit tests that
    never reach the production binding do not import Celery. The
    broker connection (Redis) is opened lazily by Celery on the first
    ``send_task`` call; the publisher itself holds no broker state.
    """

    def __init__(self, celery_app: Any) -> None:
        self._celery_app = celery_app

    def send_task(
        self,
        *,
        name: str,
        args: list,
        kwargs: dict,
        queue: str,
        task_id: str,
    ) -> str:
        # ``celery_app.send_task`` returns ``AsyncResult``; the task_id
        # is the canonical handle. We pass through the caller's
        # ``task_id`` (the ``event_id`` UUID stringified) so the
        # broker round-trip is observable as a duplicate via the
        # consumer-side ``bind_or_create_running_task_log``.
        result = self._celery_app.send_task(
            name,
            args=args,
            kwargs=kwargs,
            queue=queue,
            task_id=task_id,
        )
        return str(result.id)


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------

RetryableError = Union[
    "kombu.exceptions.OperationalError",  # type: ignore[name-defined]  # noqa: F821
    "amqp.exceptions.ConnectionError",  # type: ignore[name-defined]  # noqa: F821
    ConnectionError,
    TimeoutError,
    OSError,
]


def classify_celery_error(exc: BaseException) -> str:
    """Map a broker / Celery exception to ``retryable`` or ``terminal``.

    The mapping mirrors ADR §8 ("Failure matrix"):

    - ``celery.exceptions.NotRegistered`` → ``terminal``. The broker
      cannot accept this ``task_name``; retrying produces the same
      error. The row goes straight to ``failed`` without consuming the
      12-attempt budget.
    - ``kombu.exceptions.OperationalError``,
      ``amqp.exceptions.ConnectionError``,
      builtin ``ConnectionError``, ``TimeoutError``, ``OSError`` →
      ``retryable``. The broker is unreachable / socket-level failure;
      a later retry may succeed.
    - Everything else → ``retryable``. Safety default: the next attempt
      will hit the broker again, and the worst case is "still failing"
      rather than "row stuck in publishing forever". Operators see the
      exception in ``OutboxEvent.last_error``.
    """
    # Local imports keep Celery / kombu optional — unit tests that
    # exercise ``classify_celery_error`` directly with builtin
    # exceptions do not need them.
    try:
        from celery.exceptions import NotRegistered

        if isinstance(exc, NotRegistered):
            return "terminal"
    except ImportError:
        pass

    try:
        from kombu.exceptions import OperationalError as KombuOperationalError

        if isinstance(exc, KombuOperationalError):
            return "retryable"
    except ImportError:
        pass

    try:
        import kombu.exceptions as _kombu_exceptions

        kombu_connection_error = getattr(_kombu_exceptions, "ConnectionError", None)
        if kombu_connection_error is not None and isinstance(exc, kombu_connection_error):
            return "retryable"
    except ImportError:
        pass

    if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
        return "retryable"

    return "retryable"


def _truncate_error(exc: BaseException) -> str:
    """Return a single-line error string of bounded length.

    ``OutboxEvent.last_error`` is operator-facing; the full traceback
    should be emitted to the structured log (Todo 24). The DB column
    is ``TEXT`` with no length constraint, but logging a multi-MB
    repr of a chained exception would balloon the row size and
    obscure the underlying message — truncate aggressively.
    """
    message = f"{type(exc).__name__}: {exc}"
    if len(message) > PUBLISHER_ERROR_TRUNCATE_CHARS:
        message = message[:PUBLISHER_ERROR_TRUNCATE_CHARS] + "…[truncated]"
    return message


# ---------------------------------------------------------------------------
# Config / stats / health
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PublisherConfig:
    """Configuration for one publisher instance.

    Mirrors the constants in ADR §4 with overrides the CLI / tests can
    pass in. ``one_shot=True`` runs a single ``run_once`` and exits;
    it is the entry point used by unit / integration tests so the
    test code does not have to fight the loop.

    Attributes:
        claimed_by: Publisher instance id recorded on every claim
            (``OutboxEvent.claimed_by``). The CLI builds this from
            ``hostname + pid``; tests pass a fixed string so the
            "two publishers, no double-claim" assertion is
            deterministic.
        poll_interval_seconds: Sleep between polls when the pool is
            empty / when ``one_shot`` is False. The unit / integration
            tests set this to ``0`` so they do not have to mock
            ``time.sleep``.
        batch_size: ``LIMIT`` size for the publisher's claim query
            (passed to :meth:`OutboxRepository.try_claim_one`). The
            publisher processes one row per ``run_once`` call; the
            batch size only matters for the candidate SELECT inside
            the repository.
        lease_seconds: ``lease_expires_at = now() + lease_seconds``.
        max_attempts: Terminal-failure threshold. ``attempt_count + 1
            >= max_attempts`` triggers ``mark_failed_terminal``.
        backoff_base_seconds: Mirror of
            :data:`PUBLISHER_BACKOFF_BASE_SECONDS` for tests that want
            to override the global default.
        backoff_cap_seconds: Mirror of
            :data:`PUBLISHER_BACKOFF_CAP_SECONDS`.
        published_retention_days: Mirror of
            :data:`PUBLISHER_PUBLISHED_RETENTION_DAYS` for the
            retention helper.
        one_shot: When ``True``, :meth:`run_until_signal` runs a single
            :meth:`run_once` then returns. The CLI ``--once`` flag is
            the production entry point that uses this.
    """

    claimed_by: str
    poll_interval_seconds: int = PUBLISHER_POLL_INTERVAL_SECONDS
    batch_size: int = PUBLISHER_BATCH_SIZE
    lease_seconds: int = PUBLISHER_LEASE_SECONDS
    max_attempts: int = PUBLISHER_MAX_ATTEMPTS
    backoff_base_seconds: int = PUBLISHER_BACKOFF_BASE_SECONDS
    backoff_cap_seconds: int = PUBLISHER_BACKOFF_CAP_SECONDS
    published_retention_days: int = PUBLISHER_PUBLISHED_RETENTION_DAYS
    one_shot: bool = False


@dataclass(frozen=True)
class PublisherStats:
    """Counters produced by one ``run_once``.

    All fields are cumulative since the publisher instance started.
    The supervisor / metrics layer (Todo 24) reads these for the
    ``outbox_publish_*` Prometheus surface.

    Attributes:
        claimed: Number of rows successfully claimed from the pool.
        published: Number of rows that reached ``published``.
        retryable_failures: Number of broker errors classified as
            ``retryable``; the row went back to ``pending`` with a
            bumped ``attempt_count`` and ``next_attempt_at``.
        terminal_failures: Number of rows that reached ``failed`` —
            either a terminal broker error (``NotRegistered``) or the
            ``attempt_count`` budget was exhausted.
        lease_reclaimed: Reserved for the supervisor path; currently
            always ``0``. A future iteration could distinguish
            "claimed a row whose previous publisher's lease had
            expired" from "claimed a never-claimed row".
        empty_polls: Number of ticks ``try_claim_one`` returned
            ``None`` (no eligible row in the pool).
        last_processed_at: Wall-clock of the most recent claim /
            publish / error. ``None`` when the publisher has never
            processed a row.
    """

    claimed: int = 0
    published: int = 0
    retryable_failures: int = 0
    terminal_failures: int = 0
    lease_reclaimed: int = 0
    empty_polls: int = 0
    last_processed_at: Optional[datetime] = None

    def merge(self, other: "PublisherStats") -> "PublisherStats":
        """Combine two :class:`PublisherStats` snapshots.

        Used by :meth:`OutboxPublisher.run_until_signal` to accumulate
        counters across iterations.
        """
        last_processed_at = self.last_processed_at
        if other.last_processed_at is not None:
            if last_processed_at is None or other.last_processed_at > last_processed_at:
                last_processed_at = other.last_processed_at
        return PublisherStats(
            claimed=self.claimed + other.claimed,
            published=self.published + other.published,
            retryable_failures=self.retryable_failures + other.retryable_failures,
            terminal_failures=self.terminal_failures + other.terminal_failures,
            lease_reclaimed=self.lease_reclaimed + other.lease_reclaimed,
            empty_polls=self.empty_polls + other.empty_polls,
            last_processed_at=last_processed_at,
        )


@dataclass(frozen=True)
class PublisherHealth:
    """Result of :meth:`OutboxPublisher.health_check`.

    The supervisor / docker-compose ``healthcheck`` script (Todo 23)
    reads this to expose readiness / liveness. ``healthy`` is a
    coarse aggregate; the supervisor / dashboard decide whether the
    publisher is "ready" (healthy AND not lagged) vs "live" (just
    process-up).

    Attributes:
        healthy: ``True`` when the publisher could query the pool AND
            no exception bubbled out of ``health_check``.
        stats: The cumulative stats since the publisher started.
        oldest_pending_seconds: Wall-clock age of the oldest non-
            terminal row, measured against ``self._clock()``. ``None``
            when the pool is empty.
        lag_degraded: ``True`` when ``oldest_pending_seconds`` exceeds
            :data:`PUBLISHER_BACKLOG_DEGRADED_SECONDS`. The publisher
            **does not** delete or skip rows in response (ADR §8
            "Backlog grows > 300 s").
    """

    healthy: bool
    stats: PublisherStats
    oldest_pending_seconds: Optional[int]
    lag_degraded: bool


# ---------------------------------------------------------------------------
# Publisher
# ---------------------------------------------------------------------------


ClockCallable = Callable[[], datetime]


class OutboxPublisher:
    """The outbox publisher loop.

    Owns one :class:`OutboxRepository` against the caller-supplied
    ``db_session`` and one :class:`BrokerPort`. The session lifecycle
    is the caller's responsibility: the CLI opens a session per
    iteration (matches :func:`src.db.session.task_db_session`),
    unit tests share one session across the whole test, integration
    tests do the same with a session-bound PG connection.

    Args:
        db_session: Caller-owned SQLAlchemy session.
        config: :class:`PublisherConfig` snapshot.
        broker: :class:`BrokerPort` adapter. Tests pass a fake; the CLI
            passes a :class:`CeleryBrokerPort`.
        clock: Wall-clock source. Defaults to ``datetime.now(tz=utc)``;
            tests inject a deterministic callable so the lease /
            backoff math is reproducible.

    Notes:
        ``OutboxPublisher`` is **stateless** across ``run_once`` calls
        *except* for the cumulative :attr:`_stats` attribute, which is
        mutated in-place. This makes the loop easy to drive from tests
        — each ``run_once`` returns a snapshot of *this iteration's*
        delta; :attr:`_stats` is the cumulative view.
    """

    def __init__(
        self,
        db_session: Session,
        config: PublisherConfig,
        broker: BrokerPort,
        clock: ClockCallable = lambda: datetime.now(tz=timezone.utc),
    ) -> None:
        self._db = db_session
        self._config = config
        self._broker = broker
        self._clock = clock
        self._stats = PublisherStats()
        self._repository = OutboxRepository(db_session)

    # ------------------------------------------------------------------
    # Public surface
    # ------------------------------------------------------------------

    @property
    def stats(self) -> PublisherStats:
        """Return the cumulative stats snapshot."""
        return self._stats

    def run_once(self) -> PublisherStats:
        """Claim one row, publish it, mark it; return this iteration's delta.

        Algorithm (mirrors the brief in §4.1 of the Todo 13 contract):

        1. ``event = repo.try_claim_one(claimed_by, lease_seconds=...)``.
           If ``None``: increment ``empty_polls``, return.
        2. Try ``broker.send_task(name=event.task_name,
           args=event.args_json, kwargs=event.kwargs_json,
           queue=event.queue, task_id=str(event.event_id))``.
        3. On success: ``repo.mark_published(event.event_id)``,
           increment ``published``.
        4. On exception: ``classification = classify_celery_error(exc)``.
           - ``terminal`` OR ``event.attempt_count + 1 >=
             config.max_attempts``: ``repo.mark_failed_terminal(...)``,
             increment ``terminal_failures``.
           - Else: ``next_attempt = compute_next_attempt_at(
             event.attempt_count + 1)`` and
             ``repo.mark_failed_to_retry(...)``, increment
             ``retryable_failures``.

        Returns:
            A :class:`PublisherStats` snapshot with the deltas applied
            in this iteration only. The cumulative :attr:`stats` is
            updated in place.
        """
        claimed = self._repository.try_claim_one(
            claimed_by=self._config.claimed_by,
            lease_seconds=self._config.lease_seconds,
            batch_size=self._config.batch_size,
            now=self._clock(),
        )
        if claimed is None:
            self._stats = self._stats.merge(
                PublisherStats(empty_polls=1, last_processed_at=self._clock())
            )
            return PublisherStats(empty_polls=1, last_processed_at=self._clock())

        try:
            self._broker.send_task(
                name=claimed.task_name,
                args=list(claimed.args_json),
                kwargs=dict(claimed.kwargs_json),
                queue=claimed.queue,
                task_id=str(claimed.event_id),
            )
        except BaseException as exc:  # noqa: BLE001  (publisher must classify every failure)
            delta = self._handle_publish_error(claimed, exc)
            self._db.commit()
            return delta

        self._repository.mark_published(claimed.event_id, now=self._clock())
        self._db.commit()
        self._stats = self._stats.merge(
            PublisherStats(
                claimed=1,
                published=1,
                last_processed_at=self._clock(),
            )
        )
        return PublisherStats(
            claimed=1,
            published=1,
            last_processed_at=self._clock(),
        )

    def run_until_signal(self, stop: Callable[[], bool]) -> PublisherStats:
        """Loop :meth:`run_once` until ``stop()`` returns ``True``.

        Sleeps ``config.poll_interval_seconds`` between iterations so
        the publisher does not busy-loop on an empty pool. ``one_shot``
        mode (used by the CLI ``--once`` flag and most tests) runs a
        single ``run_once`` then returns.

        Args:
            stop: Callable returning ``True`` when the loop should
                exit. The CLI binds this to a flag flipped by SIGTERM
                / SIGINT handlers.

        Returns:
            The cumulative :class:`PublisherStats` since the
            publisher started.
        """
        if self._config.one_shot:
            self.run_once()
            return self._stats

        while not stop():
            self.run_once()
            if self._config.poll_interval_seconds > 0:
                time.sleep(self._config.poll_interval_seconds)
        return self._stats

    def health_check(self) -> PublisherHealth:
        """Snapshot the publisher's health / lag status.

            Reads the cumulative :class:`PublisherStats` and asks the
            repository for the oldest non-terminal row's age. The clock
            used to compute the age is :attr:`_clock`; in the CLI / docker
        ``healthcheck`` it is :func:`datetime.now`.

            Returns:
                A :class:`PublisherHealth` snapshot.

            Notes:
                ``healthy`` is "we could query the pool AND no exception
                bubbled out". It is intentionally cheap; the supervisor /
                docker-compose ``healthcheck`` calls it on every interval.
        """
        try:
            backlog_count = self._repository.count_pending_older_than(
                seconds=PUBLISHER_BACKLOG_DEGRADED_SECONDS
            )
            now = self._clock()
            stats = self._stats
            oldest_pending_seconds: Optional[int] = None
            lag_degraded = False
            # The repository helper above is a count, not an age; the
            # brief asked for ``oldest_pending_seconds``. Compute it
            # with a direct SELECT scoped to non-terminal rows.
            oldest_pending_seconds = self._compute_oldest_pending_seconds(now=now)
            if oldest_pending_seconds is not None:
                lag_degraded = oldest_pending_seconds > PUBLISHER_BACKLOG_DEGRADED_SECONDS
            del backlog_count  # only used to keep the helper call live
            return PublisherHealth(
                healthy=True,
                stats=stats,
                oldest_pending_seconds=oldest_pending_seconds,
                lag_degraded=lag_degraded,
            )
        except Exception as exc:  # noqa: BLE001  (health-check must never raise)
            logger.warning("outbox publisher health check failed: %s", exc)
            return PublisherHealth(
                healthy=False,
                stats=self._stats,
                oldest_pending_seconds=None,
                lag_degraded=False,
            )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _handle_publish_error(
        self,
        claimed: OutboxEvent,
        exc: BaseException,
    ) -> PublisherStats:
        """Decide retryable vs terminal and update the row + stats.

        Computes the per-iteration :class:`PublisherStats` delta,
        applies the appropriate repository transition, merges the
        delta into :attr:`_stats`, and returns the delta so the
        caller can return it from :meth:`run_once` without recomputation.
        Mirroring the delta in two places (helper return + cumulative
        merge) keeps the unit-test assertion
        (``delta.retryable_failures == 1``) meaningful while the
        cumulative ``self.stats`` is also accurate.

        Args:
            claimed: The :class:`OutboxEvent` that just failed to
                publish. ``attempt_count`` reflects the value
                :meth:`OutboxRepository.try_claim_one` left on the row
                before the broker call.
            exc: The exception raised by ``broker.send_task``.

        Returns:
            The :class:`PublisherStats` delta for this iteration.
        """
        classification = classify_celery_error(exc)
        next_attempt_count = claimed.attempt_count + 1
        error_message = _truncate_error(exc)
        now = self._clock()

        if classification == "terminal" or next_attempt_count >= self._config.max_attempts:
            logger.warning(
                "outbox publish terminal failure: event_id=%s attempt=%s err=%s",
                claimed.event_id,
                next_attempt_count,
                error_message,
            )
            self._repository.mark_failed_terminal(
                claimed.event_id,
                last_error=error_message,
                now=now,
            )
            delta = PublisherStats(
                claimed=1,
                terminal_failures=1,
                last_processed_at=now,
            )
            self._stats = self._stats.merge(delta)
            return delta

        next_attempt_at = compute_next_attempt_at(next_attempt_count)
        logger.info(
            "outbox publish retryable failure: event_id=%s attempt=%s next_attempt_at=%s err=%s",
            claimed.event_id,
            next_attempt_count,
            next_attempt_at.isoformat(),
            error_message,
        )
        self._repository.mark_failed_to_retry(
            claimed.event_id,
            next_attempt_at=next_attempt_at,
            last_error=error_message,
            now=now,
        )
        delta = PublisherStats(
            claimed=1,
            retryable_failures=1,
            last_processed_at=now,
        )
        self._stats = self._stats.merge(delta)
        return delta

    def _compute_oldest_pending_seconds(self, *, now: datetime) -> Optional[int]:
        """Return the wall-clock age of the oldest non-terminal row.

        Reads ``OutboxEvent`` rows whose status is ``pending`` or
        ``publishing`` and whose ``next_attempt_at`` is in the past
        (i.e. the publisher is currently meant to be looking at them).
        Returns ``None`` when no row matches. Uses
        :meth:`OutboxRepository.count_pending_older_than` is NOT
        enough — that helper returns a count, not an age. We issue a
        dedicated SELECT.

        Args:
            now: Reference wall-clock instant. Defaults to
                ``self._clock()``; tests inject a controlled value.
        """
        from sqlalchemy import and_, func, or_, select  # local import: avoid hard dep on caller

        from src.application.outbox.state_machine import OutboxStatus
        from src.models.outbox import OutboxEvent as OutboxEventRow

        threshold = now - timedelta(seconds=PUBLISHER_BACKLOG_DEGRADED_SECONDS)
        stmt = select(func.min(OutboxEventRow.next_attempt_at)).where(
            and_(
                OutboxEventRow.status.in_(
                    (OutboxStatus.PENDING.value, OutboxStatus.PUBLISHING.value)
                ),
                or_(
                    OutboxEventRow.next_attempt_at < threshold,
                    and_(
                        OutboxEventRow.status == OutboxStatus.PUBLISHING.value,
                        OutboxEventRow.lease_expires_at.is_not(None),
                        OutboxEventRow.lease_expires_at < threshold,
                    ),
                ),
            )
        )
        result = self._db.execute(stmt).scalar_one_or_none()
        if result is None:
            return None
        # SQLite returns naive datetimes; normalize to UTC.
        if isinstance(result, datetime) and result.tzinfo is None:
            result = result.replace(tzinfo=timezone.utc)
        return int((now - result).total_seconds())


# ---------------------------------------------------------------------------
# Public re-exports
# ---------------------------------------------------------------------------

__all__ = [
    "BrokerPort",
    "CeleryBrokerPort",
    "OutboxPublisher",
    "PUBLISHER_BACKLOG_DEGRADED_SECONDS",
    "PUBLISHER_BACKOFF_BASE_SECONDS",
    "PUBLISHER_BACKOFF_CAP_SECONDS",
    "PUBLISHER_BATCH_SIZE",
    "PUBLISHER_ERROR_TRUNCATE_CHARS",
    "PUBLISHER_LEASE_SECONDS",
    "PUBLISHER_MAX_ATTEMPTS",
    "PUBLISHER_POLL_INTERVAL_SECONDS",
    "PUBLISHER_PUBLISHED_RETENTION_DAYS",
    "PublisherConfig",
    "PublisherHealth",
    "PublisherStats",
    "classify_celery_error",
    "compute_next_attempt_at",
]
