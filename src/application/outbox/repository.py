"""Outbox repository — the SQLAlchemy bridge for ``outbox_event``.

This module owns the **persistence-side** mechanics of the
transactional outbox defined in ADR
``docs/adr/0011-transactional-outbox-and-task-lifecycle.md``. The
DTO contract (Todo 11) is the in-memory shape use cases / tests
reason about; this module is the SQLAlchemy implementation that:

- turns an :class:`OutboxCommand` plus a ``TaskLog`` row into a
  single ``INSERT … ON CONFLICT DO NOTHING`` statement that runs in
  the **caller's transaction** so the business write and the outbox
  row commit atomically;
- lets the publisher (Todo 13) claim a pending row via
  ``SELECT … FOR UPDATE SKIP LOCKED``;
- drives the state machine through ``mark_published`` /
  ``mark_failed_to_retry`` / ``mark_failed_terminal``;
- exposes retention / backlog helpers ``cleanup_published_older_than``
  and ``count_pending_older_than`` for the supervisor (Todo 13) and
  the operations runbook (Todo 24).

Atomicity contract
==================

:func:`enroll_with_task_log` is the load-bearing entry point. The
repository **does not** open a new transaction; it adds the
``OutboxEvent`` to the caller-supplied ``db_session`` and relies on
the caller to ``commit()``. This is the seam the cutover (Todo 14)
relies on: business code commits ``TaskLog`` + ``OutboxEvent`` in one
transaction; a crash anywhere between the two leaves zero rows.

SQLite vs PostgreSQL
====================

The repository deliberately supports both dialects:

- on **PostgreSQL**, the partial unique index
  ``(task_log_id) WHERE status='pending'`` is the structural
  guarantee that prevents duplicate enqueue under concurrency;
  ``INSERT … ON CONFLICT (task_log_id) WHERE status='pending' DO
  NOTHING`` returns ``0 rows`` when a parallel transaction already
  inserted one, and the caller falls back to "the original row is
  the one to publish".
- on **SQLite**, there is no partial-index-aware ``ON CONFLICT``;
  the repository falls back to a regular ``INSERT … ON CONFLICT
  (task_log_id) DO NOTHING``. The full ``UNIQUE (task_log_id)``
  constraint (also part of the schema) still prevents duplicate
  rows, so the SQLite path is safe for unit tests that exercise
  the contract without standing up PostgreSQL.

Concurrency notes
=================

``try_claim_one`` uses ``SELECT … FOR UPDATE SKIP LOCKED`` on
PostgreSQL so multiple publisher instances do not contend on the
same row. The clause is PostgreSQL-only; on SQLite the method
behaves as a plain SELECT + UPDATE inside an explicit transaction.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import and_, delete, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.application.outbox.contracts import (
    OutboxCommand,
    OutboxEvent,
    OutboxStatus,
    emit_event,
)
from src.models.outbox import PENDING_INDEX_PREDICATE
from src.models.outbox import OutboxEvent as OutboxEventRow
from src.models.task_log import TaskLog

# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EnrollOutcome:
    """Result of :meth:`OutboxRepository.enroll_with_task_log`.

    Attributes:
        event: The persisted ``OutboxEvent``. ``event.id`` is filled
            in by the BIGSERIAL sequence on PostgreSQL (or the
            SQLite rowid on SQLite).
        created: True if a new row was INSERTed; False if a row for
            the same ``task_log_id`` already existed and the caller
            should treat the existing row as authoritative.
    """

    event: OutboxEvent
    created: bool


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------


class OutboxRepository:
    """Persistence bridge for ``outbox_event``.

    Instances are cheap: they hold only the ``Session``. Use one per
    request / worker tick / unit-test transaction; do not share across
    threads because ``Session`` is not thread-safe.
    """

    # Default publisher lease length (matches ADR §4 "Retry parameters").
    DEFAULT_LEASE_SECONDS: int = 60

    def __init__(self, db_session: Session) -> None:
        self._db = db_session

    # ------------------------------------------------------------------
    # Enqueue (atomic with TaskLog)
    # ------------------------------------------------------------------

    def enroll_with_task_log(
        self,
        task_log: TaskLog,
        command: OutboxCommand,
        *,
        dedupe_key: Optional[str] = None,
    ) -> EnrollOutcome:
        """Persist a new outbox row bound to ``task_log`` in the caller tx.

        Uses ``INSERT … ON CONFLICT DO NOTHING`` against the partial
        unique index on ``(task_log_id) WHERE status='pending'`` (PG)
        or the full unique on ``task_log_id`` (SQLite). The row is
        added to ``self._db`` but **not** committed; the caller owns
        the transaction so the business write and the outbox INSERT
        share one commit.

        Args:
            task_log: The ``TaskLog`` row that was just created in the
                same transaction. ``task_log.id`` MUST be populated.
            command: The :class:`OutboxCommand` describing the
                Celery task to publish.
            dedupe_key: Optional mirror of ``task_log.dedupe_key``;
                defaults to ``task_log.dedupe_key``.

        Returns:
            :class:`EnrollOutcome`. ``event.event_id`` is the value
            the publisher will use as the Celery ``task_id``; pass it
            back to the caller so it can be correlated with the
            consumer-side bind helper.

        Raises:
            ValueError: when ``task_log.id`` is ``None`` (the row was
                not flushed yet).
            :class:`OutboxContractViolation`: when the payload or
                ``task_name`` fails the registry / JSON-safety rules
                from ADR §5 / §6.
        """
        if task_log.id is None:
            raise ValueError(
                "task_log.id must be populated before enroll_with_task_log; "
                "call db.flush() on the caller session first"
            )
        resolved_dedupe_key = dedupe_key if dedupe_key is not None else task_log.dedupe_key
        # ``emit_event`` runs the registry / payload / duplicate-event_id
        # validation from ADR §5 / §6 / §10. Re-raise on violation so
        # the caller can roll back the business tx.
        event = emit_event(
            command=command,
            task_log_id=task_log.id,
            dedupe_key=resolved_dedupe_key,
        )

        row_values = self._row_values_from_event(event)

        if self._is_postgres():
            insert_stmt: Any = postgresql_insert(OutboxEventRow).values(**row_values)
            insert_stmt = insert_stmt.on_conflict_do_nothing(
                index_elements=["task_log_id"],
                index_where=text(PENDING_INDEX_PREDICATE),
            )
            result = self._db.execute(
                insert_stmt.returning(OutboxEventRow.event_id, OutboxEventRow.id)
            )
            returned = result.mappings().first()
            if returned is None:
                # A pending row already exists; caller treats the existing
                # row as authoritative. We still need an event_id to hand
                # back to the caller (the consumer-side bind helper uses
                # it to look up the row), so we read it from the DB.
                existing = self._db.execute(
                    select(OutboxEventRow).where(OutboxEventRow.task_log_id == task_log.id)
                ).scalar_one()
                return EnrollOutcome(
                    event=self._event_from_row(existing),
                    created=False,
                )
            self._db.flush()
            event_id = returned["event_id"]
            inserted_id = returned["id"]
            # Backfill the dataclass so callers see the DB-assigned
            # surrogate id and the canonical event_id value.
            object.__setattr__(event, "id", inserted_id)
            object.__setattr__(event, "event_id", event_id)
            return EnrollOutcome(event=event, created=True)

        # SQLite path: regular UNIQUE on ``task_log_id`` (no partial).
        # Still race-safe at the unit-test level because the index is
        # enforced. SAVEPOINT wraps the INSERT so an IntegrityError
        # cannot poison the caller's outer transaction.
        insert_stmt = sqlite_insert(OutboxEventRow).values(**row_values)
        insert_stmt = insert_stmt.on_conflict_do_nothing(index_elements=["task_log_id"])
        nested = self._db.begin_nested()
        try:
            result = self._db.execute(insert_stmt)
            nested.commit()
        except IntegrityError:
            nested.rollback()
            existing = self._db.execute(
                select(OutboxEventRow).where(OutboxEventRow.task_log_id == task_log.id)
            ).scalar_one()
            return EnrollOutcome(
                event=self._event_from_row(existing),
                created=False,
            )
        if _rowcount(result) == 0:
            # ON CONFLICT DO NOTHING consumed a duplicate.
            existing = self._db.execute(
                select(OutboxEventRow).where(OutboxEventRow.task_log_id == task_log.id)
            ).scalar_one()
            return EnrollOutcome(
                event=self._event_from_row(existing),
                created=False,
            )
        self._db.flush()
        # SQLite increments the implicit ``rowid`` into ``id``; refresh
        # from the row so the dataclass is consistent.
        inserted = self._db.execute(
            select(OutboxEventRow).where(OutboxEventRow.task_log_id == task_log.id)
        ).scalar_one()
        object.__setattr__(event, "id", inserted.id)
        object.__setattr__(event, "event_id", inserted.event_id)
        return EnrollOutcome(event=event, created=True)

    # ------------------------------------------------------------------
    # Publisher hot path
    # ------------------------------------------------------------------

    def try_claim_one(
        self,
        claimed_by: str,
        *,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        batch_size: int = 100,
        now: Optional[datetime] = None,
    ) -> Optional[OutboxEvent]:
        """Atomically claim one pending row for the publisher.

        Selects up to ``batch_size`` pending rows whose
        ``next_attempt_at`` is in the past, picks the first one, and
        updates it to ``status='publishing'`` with the supplied
        ``claimed_by`` and ``lease_expires_at = now() + lease_seconds``.

        On PostgreSQL the SELECT uses ``FOR UPDATE SKIP LOCKED`` so
        two publisher instances never contend. The whole operation
        runs in a single transaction so the lease + status flip are
        atomic.

        Returns ``None`` when no row is eligible. Returns the claimed
        :class:`OutboxEvent` (with the post-update status /
        ``claimed_by`` / ``lease_expires_at``) otherwise.
        """
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        current = now or datetime.now(tz=timezone.utc)
        lease_expiry = current + timedelta(seconds=lease_seconds)

        # Pick the candidate set under a row lock so concurrent
        # publishers do not claim the same id. ``with_for_update`` on
        # SQLite is a no-op (SQLite serializes writers anyway).
        candidate_stmt = (
            select(OutboxEventRow)
            .where(
                OutboxEventRow.status == OutboxStatus.PENDING.value,
                OutboxEventRow.next_attempt_at <= current,
            )
            .order_by(OutboxEventRow.next_attempt_at, OutboxEventRow.id)
            .limit(batch_size)
        )
        if self._is_postgres():
            candidate_stmt = candidate_stmt.with_for_update(skip_locked=True)
        candidates = self._db.execute(candidate_stmt).scalars().all()
        if not candidates:
            return None
        chosen = candidates[0]

        update_stmt = (
            update(OutboxEventRow)
            .where(
                OutboxEventRow.id == chosen.id,
                OutboxEventRow.status == OutboxStatus.PENDING.value,
            )
            .values(
                status=OutboxStatus.PUBLISHING.value,
                claimed_by=claimed_by,
                lease_expires_at=lease_expiry,
                updated_at=current,
            )
        )
        result = self._db.execute(update_stmt)
        if _rowcount(result) == 0:
            # Someone else claimed it between SELECT and UPDATE; the
            # publisher loop just retries next tick.
            return None
        self._db.flush()
        self._db.expire(chosen)
        refreshed = self._db.execute(
            select(OutboxEventRow).where(OutboxEventRow.id == chosen.id)
        ).scalar_one()
        return self._event_from_row(refreshed)

    def mark_published(
        self,
        event_id: Any,
        *,
        now: Optional[datetime] = None,
    ) -> Optional[OutboxEvent]:
        """Move a row from ``publishing`` → ``published``.

        Sets ``published_at`` to ``now()`` and clears ``claimed_by`` /
        ``lease_expires_at``. The UPDATE is conditional on the row
        still being in ``publishing`` and the lease still owned by the
        caller — if those guards fail (lease expired or another
        publisher took over) the function returns ``None`` so the
        caller can decide whether to retry / give up.
        """
        current = now or datetime.now(tz=timezone.utc)
        result = self._db.execute(
            update(OutboxEventRow)
            .where(
                OutboxEventRow.event_id == event_id,
                OutboxEventRow.status == OutboxStatus.PUBLISHING.value,
            )
            .values(
                status=OutboxStatus.PUBLISHED.value,
                published_at=current,
                claimed_by=None,
                lease_expires_at=None,
                last_error=None,
                updated_at=current,
            )
        )
        if _rowcount(result) == 0:
            return None
        self._db.flush()
        refreshed = self._db.execute(
            select(OutboxEventRow).where(OutboxEventRow.event_id == event_id)
        ).scalar_one()
        return self._event_from_row(refreshed)

    def mark_failed_to_retry(
        self,
        event_id: Any,
        *,
        next_attempt_at: datetime,
        last_error: str,
        now: Optional[datetime] = None,
    ) -> Optional[OutboxEvent]:
        """Move a row from ``publishing`` → ``pending`` for retry.

        Increments ``attempt_count`` and sets ``next_attempt_at`` to
        the caller-supplied value (the publisher computes the backoff
        using the schedule in ADR §4). Clears ``claimed_by`` /
        ``lease_expires_at``.
        """
        current = now or datetime.now(tz=timezone.utc)
        result = self._db.execute(
            update(OutboxEventRow)
            .where(
                OutboxEventRow.event_id == event_id,
                OutboxEventRow.status == OutboxStatus.PUBLISHING.value,
            )
            .values(
                status=OutboxStatus.PENDING.value,
                attempt_count=OutboxEventRow.attempt_count + 1,
                next_attempt_at=next_attempt_at,
                claimed_by=None,
                lease_expires_at=None,
                last_error=last_error,
                updated_at=current,
            )
        )
        if _rowcount(result) == 0:
            return None
        self._db.flush()
        refreshed = self._db.execute(
            select(OutboxEventRow).where(OutboxEventRow.event_id == event_id)
        ).scalar_one()
        return self._event_from_row(refreshed)

    def mark_failed_terminal(
        self,
        event_id: Any,
        *,
        last_error: str,
        now: Optional[datetime] = None,
    ) -> Optional[OutboxEvent]:
        """Move a row from ``publishing`` → ``failed`` after max attempts.

        Per ADR §3, the publisher takes the terminal path when
        ``attempt_count >= 12`` (or when the broker rejects the
        message with a non-retryable error). Clears ``claimed_by`` /
        ``lease_expires_at`` so the row is not reclaimable by a future
        lease-expiry sweep.
        """
        current = now or datetime.now(tz=timezone.utc)
        result = self._db.execute(
            update(OutboxEventRow)
            .where(
                OutboxEventRow.event_id == event_id,
                OutboxEventRow.status == OutboxStatus.PUBLISHING.value,
            )
            .values(
                status=OutboxStatus.FAILED.value,
                claimed_by=None,
                lease_expires_at=None,
                last_error=last_error,
                updated_at=current,
            )
        )
        if _rowcount(result) == 0:
            return None
        self._db.flush()
        refreshed = self._db.execute(
            select(OutboxEventRow).where(OutboxEventRow.event_id == event_id)
        ).scalar_one()
        return self._event_from_row(refreshed)

    # ------------------------------------------------------------------
    # Retention / supervision
    # ------------------------------------------------------------------

    def cleanup_published_older_than(
        self,
        older_than: datetime,
    ) -> int:
        """Delete ``published`` rows whose ``published_at < older_than``.

        Per ADR §4 the retention is 30 days; the publisher (Todo 13)
        and the operations runbook (Todo 24) call this on a clock.
        Returns the row count for logging / metric purposes.
        """
        result = self._db.execute(
            delete(OutboxEventRow).where(
                and_(
                    OutboxEventRow.status == OutboxStatus.PUBLISHED.value,
                    OutboxEventRow.published_at.is_not(None),
                    OutboxEventRow.published_at < older_than,
                )
            )
        )
        return _rowcount(result)

    def count_pending_older_than(self, seconds: int) -> int:
        """Count non-terminal rows whose age exceeds ``seconds``.

        Per ADR §4 the degraded threshold is 300 s; the supervisor
        (Todo 13) uses this to emit a metric when the backlog grows.
        ``pending`` and ``publishing`` both count — the publisher
        does **not** skip rows, it only logs.
        """
        threshold = datetime.now(tz=timezone.utc) - timedelta(seconds=seconds)
        stmt = select(func.count(OutboxEventRow.id)).where(
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
        return int(self._db.execute(stmt).scalar_one() or 0)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _is_postgres(self) -> bool:
        bind = self._db.bind
        return bool(bind is not None and bind.dialect.name == "postgresql")

    def _row_values_from_event(self, event: OutboxEvent) -> dict[str, Any]:
        """Translate the DTO into a ``dict`` for the INSERT.

        Args/kwargs are pre-validated and JSON-normalized by
        :func:`emit_event` so we can store them as-is; PostgreSQL
        will serialize them into ``JSONB`` and SQLite will serialize
        into ``TEXT``. The DB does the final encoding so we keep the
        in-memory shape (lists/dicts) rather than pre-dumping to a
        string.
        """
        now = event.created_at or datetime.now(tz=timezone.utc)
        return {
            "event_id": event.event_id,
            "task_log_id": event.task_log_id,
            "dedupe_key": event.dedupe_key,
            "task_name": event.task_name,
            "queue": event.queue,
            "args_json": event.args_json,
            "kwargs_json": event.kwargs_json,
            "status": event.status.value
            if isinstance(event.status, OutboxStatus)
            else event.status,
            "attempt_count": event.attempt_count,
            "next_attempt_at": event.next_attempt_at or now,
            "claimed_by": event.claimed_by,
            "lease_expires_at": event.lease_expires_at,
            "published_at": event.published_at,
            "last_error": event.last_error,
            "created_at": now,
            "updated_at": now,
        }

    def _event_from_row(self, row: OutboxEventRow) -> OutboxEvent:
        """Read a row back into the DTO shape.

        Handles the dialect difference for JSONB vs TEXT: on PostgreSQL
        SQLAlchemy returns a Python ``list`` / ``dict`` already; on
        SQLite the values come back as JSON strings, so we ``json.loads``
        them.
        """
        args_json = row.args_json
        kwargs_json = row.kwargs_json
        if isinstance(args_json, str):
            args_json = json.loads(args_json)
        if isinstance(kwargs_json, str):
            kwargs_json = json.loads(kwargs_json)
        return OutboxEvent(
            id=row.id,
            event_id=row.event_id,
            task_log_id=row.task_log_id,
            dedupe_key=row.dedupe_key,
            task_name=row.task_name,
            queue=row.queue,
            args_json=args_json,
            kwargs_json=kwargs_json,
            status=OutboxStatus(row.status),
            attempt_count=row.attempt_count,
            next_attempt_at=row.next_attempt_at,
            claimed_by=row.claimed_by,
            lease_expires_at=row.lease_expires_at,
            published_at=row.published_at,
            last_error=row.last_error,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


def _rowcount(result: Any) -> int:
    """Return ``result.rowcount`` as an int, defaulting to ``0``.

    Mypy stubs type :class:`Result` as not exposing ``rowcount``; the
    runtime always populates it, so we read it via ``getattr`` with
    a default. Used by every UPDATE / DELETE / INSERT statement in
    this module.
    """
    raw = getattr(result, "rowcount", 0) or 0
    return int(raw)


__all__ = ["EnrollOutcome", "OutboxRepository"]
