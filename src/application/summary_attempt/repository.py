"""Daily-summary attempt repository — the SQLAlchemy bridge.

This module owns the persistence mechanics for
``daily_summary_generation_attempt`` (see
:mod:`src.models.daily_summary_attempt` for the field / index
contract). It is the *only* place that writes the ``status`` column,
so the state machine in
:mod:`src.application.summary_attempt.state_machine` and the SQL
guards stay in lockstep.

Responsibilities
================

- :meth:`DailySummaryAttemptRepository.claim` — take the per-date
  active slot via ``INSERT … ON CONFLICT DO NOTHING`` against the
  partial unique index ``(summary_date) WHERE status IN
  ('claimed','queued','running')``. This is the concurrency primitive
  that replaces the old "insert a bare ``DailySummary`` row as a lock"
  trick in ``src/tasks/summarizer.py``.
- ``mark_queued`` / ``mark_running`` / ``mark_succeeded`` /
  ``mark_failed`` / ``mark_cancelled`` / ``mark_timed_out`` /
  ``mark_superseded`` — drive the state machine with conditional
  ``UPDATE … WHERE id = :id AND status IN (:legal_sources)``
  statements. The legal source set is derived from
  :func:`~src.application.summary_attempt.state_machine.allowed_sources_for`,
  so an illegal transition (``claimed → succeeded``) matches zero rows
  and returns ``None`` instead of corrupting the history.
- ``find_active_for_date`` / ``find_terminal_for_date`` /
  ``next_attempt_no`` — the read paths used by the scheduler
  ("is a generation already in flight?"), the audit UI ("show me every
  attempt for this date") and the retry path ("what is the next
  attempt number?").

Transaction contract
====================

The repository **never commits**. Every method operates on the
caller-supplied ``Session`` so the attempt row can be written in the
same transaction as the business write (the ``DailySummary`` upsert and
the outbox row in Todo 16). ``flush()`` is called where the caller
needs server-assigned values, never ``commit()``.

Dialect notes
=============

- On **PostgreSQL**, ``claim`` uses
  ``INSERT … ON CONFLICT (summary_date) WHERE status IN (…) DO NOTHING
  RETURNING id``: zero returned rows means a parallel transaction
  already holds the active slot.
- On **SQLite** (unit tests), the same statement is emitted through
  the SQLite dialect's ``on_conflict_do_nothing`` — SQLite has
  supported partial-index conflict targets since 3.24 — and the
  insert / conflict decision is read from ``rowcount`` because the
  SQLite branch does not use ``RETURNING`` (keeping the supported
  SQLite floor at 3.24 rather than 3.35).
- ``mark_*`` take a plain row lock (``FOR UPDATE``, **not**
  ``SKIP LOCKED``) on PostgreSQL before the conditional UPDATE. The
  publisher's claim loop wants ``SKIP LOCKED`` because skipping a
  contended row is free (another worker owns it); a finalizer must
  *wait* for the lock instead, otherwise a concurrent cancel would
  silently no-op and the attempt would be left dangling in
  ``running`` forever.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Optional

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from src.application.summary_attempt.errors import DailySummaryAttemptError
from src.application.summary_attempt.state_machine import (
    ACTIVE_STATUSES,
    TERMINAL_STATUSES,
    DailySummaryAttemptStatus,
    allowed_sources_for,
)
from src.models.daily_summary_attempt import (
    ACTIVE_STATUS_PREDICATE,
    DailySummaryGenerationAttempt,
)

#: How many times :meth:`DailySummaryAttemptRepository.claim` retries
#: the insert / lookup pair before giving up. A retry is needed only in
#: the narrow window where the conflicting active attempt reaches a
#: terminal status between our ``ON CONFLICT DO NOTHING`` (which
#: skipped) and our follow-up lookup (which then finds nothing active).
#: Two extra passes is generous: each pass requires a *different*
#: concurrent transaction to win and immediately finish.
_CLAIM_MAX_PASSES = 3

#: Failure reasons written by the terminal helpers. ``llm_error`` and
#: ``no_events`` are supplied by the caller (only it knows why the
#: generation failed); ``cancelled`` / ``timeout`` are implied by the
#: transition itself so the helpers set them.
FAILURE_REASON_CANCELLED = "cancelled"
FAILURE_REASON_TIMEOUT = "timeout"


@dataclass(frozen=True)
class DailySummaryAttemptOutcome:
    """Result of :meth:`DailySummaryAttemptRepository.claim`.

    Attributes:
        attempt: The attempt row that now owns the per-date active
            slot. When ``created`` is ``False`` this is the row a
            *parallel* caller inserted, not the one we tried to write.
        created: ``True`` if this call inserted a new row; ``False`` if
            an active attempt already existed and the caller should
            treat that row as authoritative (i.e. back off — someone
            else is already generating this date).
    """

    attempt: DailySummaryGenerationAttempt
    created: bool


class DailySummaryAttemptRepository:
    """Persistence bridge for ``daily_summary_generation_attempt``.

    Instances are cheap — they hold only the ``Session``. Use one per
    task run / request / unit-test transaction; do not share across
    threads because ``Session`` is not thread-safe.
    """

    def __init__(self, db_session: Session) -> None:
        self._db = db_session

    # ------------------------------------------------------------------
    # Claim (per-date active slot)
    # ------------------------------------------------------------------

    def claim(
        self,
        *,
        summary_date: date,
        attempt_no: int,
        triggered_by: Optional[str],
        task_log_id: Optional[int],
    ) -> DailySummaryAttemptOutcome:
        """Attempt to claim a new active attempt for ``summary_date``.

        Uses ``INSERT … ON CONFLICT DO NOTHING`` against the partial
        unique index ``(summary_date) WHERE status IN
        ('claimed','queued','running')``. Two concurrent callers
        therefore cannot both take the slot: the loser sees zero
        inserted rows and gets the winner's row back with
        ``created=False``.

        The row is added to the caller's session but **not** committed;
        the caller owns the transaction.

        Args:
            summary_date: The date the summary is being generated for.
            attempt_no: The per-date counter for this attempt. Callers
                get it from :meth:`next_attempt_no`; it is passed
                explicitly so the caller can compute it inside the same
                transaction that performs the claim.
            triggered_by: ``"schedule"`` / ``"manual"`` / ``"retry"``,
                or ``None`` when the trigger is unknown.
            task_log_id: The correlated ``task_log.id``, or ``None``
                for an ad-hoc run that has no TaskLog.

        Returns:
            :class:`DailySummaryAttemptOutcome`.

        Raises:
            ValueError: when ``attempt_no`` is not positive.
            :class:`DailySummaryAttemptError`: when the conflicting
                active attempt keeps disappearing before it can be
                read back (see :data:`_CLAIM_MAX_PASSES`).
        """
        if attempt_no <= 0:
            raise ValueError(f"attempt_no must be positive, got {attempt_no}")

        now = datetime.now(tz=timezone.utc)
        values: dict[str, Any] = {
            "summary_date": summary_date,
            "attempt_no": attempt_no,
            "status": DailySummaryAttemptStatus.CLAIMED.value,
            "task_log_id": task_log_id,
            "triggered_by": triggered_by,
            "started_at": now,
            "claimed_at": now,
            "created_at": now,
            "updated_at": now,
        }

        for _ in range(_CLAIM_MAX_PASSES):
            inserted_id = self._insert_claim(values)
            if inserted_id is not None:
                self._db.flush()
                return DailySummaryAttemptOutcome(
                    attempt=self._require_row(inserted_id),
                    created=True,
                )
            existing = self.find_active_for_date(summary_date)
            if existing is not None:
                return DailySummaryAttemptOutcome(attempt=existing, created=False)
            # The conflicting attempt went terminal between our INSERT
            # and this lookup; the slot is free again, so retry.
        raise DailySummaryAttemptError(
            f"could not claim a daily summary attempt for {summary_date} "
            f"after {_CLAIM_MAX_PASSES} passes: the active attempt kept "
            "reaching a terminal status between the insert and the lookup",
            payload={"summary_date": str(summary_date), "attempt_no": attempt_no},
        )

    # ------------------------------------------------------------------
    # State transitions
    # ------------------------------------------------------------------

    def mark_queued(self, attempt_id: int) -> Optional[DailySummaryGenerationAttempt]:
        """Move ``claimed`` → ``queued`` (the broker accepted the run)."""
        return self._transition(attempt_id, DailySummaryAttemptStatus.QUEUED)

    def mark_running(
        self,
        attempt_id: int,
        *,
        events_count: Optional[int] = None,
        input_token_estimate: Optional[int] = None,
    ) -> Optional[DailySummaryGenerationAttempt]:
        """Move ``claimed`` / ``queued`` → ``running``.

        ``events_count`` / ``input_token_estimate`` snapshot the LLM
        inputs as they were when generation started, so a post-mortem
        on a failed attempt can tell "the model choked on 4 000 events"
        apart from "the model was called with nothing".
        """
        extra: dict[str, Any] = {}
        if events_count is not None:
            extra["events_count"] = events_count
        if input_token_estimate is not None:
            extra["input_token_estimate"] = input_token_estimate
        return self._transition(attempt_id, DailySummaryAttemptStatus.RUNNING, values=extra)

    def mark_succeeded(self, attempt_id: int) -> Optional[DailySummaryGenerationAttempt]:
        """Move ``running`` → ``succeeded`` and stamp ``finished_at``."""
        return self._transition(
            attempt_id,
            DailySummaryAttemptStatus.SUCCEEDED,
            values={
                # A succeeded attempt carries no diagnostics: clear any
                # residue a prior in-flight write may have left.
                "error_type": None,
                "last_error": None,
                "failure_reason": None,
            },
            stamp_finished=True,
        )

    def mark_failed(
        self,
        attempt_id: int,
        *,
        error_type: str,
        last_error: str,
        failure_reason: str,
    ) -> Optional[DailySummaryGenerationAttempt]:
        """Move ``running`` → ``failed`` with operator-facing diagnostics.

        ``failure_reason`` is the caller's coarse classification
        (``llm_error`` / ``no_events`` / …); ``error_type`` is the
        exception class name and ``last_error`` the truncated message.
        Keeping all three means the failure survives ``task_log``
        cleanup, which is the whole point of this table.
        """
        return self._transition(
            attempt_id,
            DailySummaryAttemptStatus.FAILED,
            values={
                "error_type": error_type,
                "last_error": last_error,
                "failure_reason": failure_reason,
            },
            stamp_finished=True,
        )

    def mark_cancelled(
        self,
        attempt_id: int,
        *,
        last_error: str,
    ) -> Optional[DailySummaryGenerationAttempt]:
        """Move ``claimed`` / ``queued`` / ``running`` → ``cancelled``.

        A cancel is an operator action, not a defect: ``error_type``
        stays ``None`` and ``failure_reason`` is fixed to
        ``cancelled`` so dashboards can exclude it from the failure
        rate.
        """
        return self._transition(
            attempt_id,
            DailySummaryAttemptStatus.CANCELLED,
            values={
                "last_error": last_error,
                "failure_reason": FAILURE_REASON_CANCELLED,
            },
            stamp_finished=True,
        )

    def mark_timed_out(
        self,
        attempt_id: int,
        *,
        last_error: str,
    ) -> Optional[DailySummaryGenerationAttempt]:
        """Move ``queued`` / ``running`` → ``timed_out``.

        Distinct from ``failed`` on purpose: a timeout means the run
        never reported back (worker killed, LLM hung), so there is no
        exception to record and the retry policy differs. ``claimed``
        has no timeout edge — a row that never reached a worker is
        superseded, not timed out.
        """
        return self._transition(
            attempt_id,
            DailySummaryAttemptStatus.TIMED_OUT,
            values={
                "last_error": last_error,
                "failure_reason": FAILURE_REASON_TIMEOUT,
            },
            stamp_finished=True,
        )

    def mark_superseded(self, attempt_id: int) -> Optional[DailySummaryGenerationAttempt]:
        """Move any active state → ``superseded``.

        Used when a newer attempt must take over the date (operator
        re-generate, recovery sweep). The row keeps whatever
        diagnostics it had; ``failure_reason`` stays ``None`` because
        being superseded is not a failure of this attempt.
        """
        return self._transition(
            attempt_id,
            DailySummaryAttemptStatus.SUPERSEDED,
            stamp_finished=True,
        )

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def find_active_for_date(self, summary_date: date) -> Optional[DailySummaryGenerationAttempt]:
        """Return the active attempt for ``summary_date``, if any.

        "Active" is ``claimed`` / ``queued`` / ``running`` — exactly the
        partial unique index predicate, so at most one row can match.
        The ``ORDER BY attempt_no DESC`` is defensive: it makes the
        result deterministic even on a database whose partial index was
        dropped out of band.
        """
        stmt = (
            select(DailySummaryGenerationAttempt)
            .where(
                DailySummaryGenerationAttempt.summary_date == summary_date,
                DailySummaryGenerationAttempt.status.in_(
                    sorted(status.value for status in ACTIVE_STATUSES)
                ),
            )
            .order_by(DailySummaryGenerationAttempt.attempt_no.desc())
            .limit(1)
            .execution_options(populate_existing=True)
        )
        return self._db.execute(stmt).scalars().first()

    def find_terminal_for_date(self, summary_date: date) -> list[DailySummaryGenerationAttempt]:
        """Return every finished attempt for ``summary_date``, oldest first.

        This is the audit / post-mortem query path backed by the
        ``(summary_date, attempt_no)`` index: "show me the history of
        what we tried for this date, and why each attempt ended".
        """
        stmt = (
            select(DailySummaryGenerationAttempt)
            .where(
                DailySummaryGenerationAttempt.summary_date == summary_date,
                DailySummaryGenerationAttempt.status.in_(
                    sorted(status.value for status in TERMINAL_STATUSES)
                ),
            )
            .order_by(DailySummaryGenerationAttempt.attempt_no.asc())
            .execution_options(populate_existing=True)
        )
        return list(self._db.execute(stmt).scalars().all())

    def next_attempt_no(self, summary_date: date) -> int:
        """Return ``max(attempt_no) + 1`` for ``summary_date``; 1 if none.

        The counter is **per date** rather than global so the audit
        query reads naturally ("attempt 2 of 2026-09-01") and so two
        dates being generated in parallel do not interleave their
        numbering.
        """
        current_max = self._db.execute(
            select(func.max(DailySummaryGenerationAttempt.attempt_no)).where(
                DailySummaryGenerationAttempt.summary_date == summary_date
            )
        ).scalar_one_or_none()
        if current_max is None:
            return 1
        return int(current_max) + 1

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _is_postgres(self) -> bool:
        bind = self._db.bind
        return bool(bind is not None and bind.dialect.name == "postgresql")

    def _insert_claim(self, values: dict[str, Any]) -> Optional[int]:
        """Run the ``ON CONFLICT DO NOTHING`` insert; return the new id.

        Returns ``None`` when the insert was skipped because another
        active attempt already holds the ``summary_date`` slot. The two
        dialect branches differ only in how the outcome is read back:
        PostgreSQL uses ``RETURNING id`` (no row → conflict); SQLite
        uses ``rowcount`` plus ``inserted_primary_key`` so the SQLite
        floor stays at 3.24 (``ON CONFLICT``) instead of 3.35
        (``RETURNING``).

        A conflict on any *other* constraint (e.g. the FK to
        ``task_log``) is deliberately **not** caught here: swallowing it
        would make an invalid ``task_log_id`` look like a lost race.
        """
        if self._is_postgres():
            pg_stmt = (
                postgresql_insert(DailySummaryGenerationAttempt)
                .values(**values)
                .on_conflict_do_nothing(
                    index_elements=["summary_date"],
                    index_where=_active_index_predicate(),
                )
                .returning(DailySummaryGenerationAttempt.id)
            )
            returned = self._db.execute(pg_stmt).mappings().first()
            if returned is None:
                return None
            return int(returned["id"])

        sqlite_stmt = (
            sqlite_insert(DailySummaryGenerationAttempt)
            .values(**values)
            .on_conflict_do_nothing(
                index_elements=["summary_date"],
                index_where=_active_index_predicate(),
            )
        )
        result = self._db.execute(sqlite_stmt)
        if _rowcount(result) == 0:
            return None
        # mypy stubs do not expose ``inserted_primary_key`` on ``Result``;
        # the runtime populates it (mirrors the ``_rowcount`` helper above).
        primary_key = getattr(result, "inserted_primary_key", None)
        if primary_key is None or primary_key[0] is None:
            return None
        return int(primary_key[0])

    def _transition(
        self,
        attempt_id: int,
        target: DailySummaryAttemptStatus,
        *,
        values: Optional[dict[str, Any]] = None,
        stamp_finished: bool = False,
    ) -> Optional[DailySummaryGenerationAttempt]:
        """Conditionally flip ``attempt_id`` to ``target``.

        The ``WHERE status IN (:legal_sources)`` guard is derived from
        the transition table, so this single helper enforces the whole
        state machine: an illegal transition (or a row already in a
        terminal status) matches zero rows and yields ``None`` rather
        than raising. Callers treat ``None`` as "someone else already
        finalized this attempt", which is a normal race, not a bug.

        On PostgreSQL the row is locked with a plain ``FOR UPDATE``
        first so two finalizers serialize instead of one silently
        losing (``SKIP LOCKED`` would be wrong here — see the module
        docstring).
        """
        sources = allowed_sources_for(target)
        if not sources:
            raise DailySummaryAttemptError(
                f"no legal source state transitions into {target.value}",
                payload={"target": target.value},
            )

        now = datetime.now(tz=timezone.utc)
        source_values = sorted(status.value for status in sources)

        if self._is_postgres():
            lock_stmt = (
                select(DailySummaryGenerationAttempt.id)
                .where(DailySummaryGenerationAttempt.id == attempt_id)
                .with_for_update()
            )
            if self._db.execute(lock_stmt).scalars().first() is None:
                return None

        update_values: dict[str, Any] = dict(values or {})
        if stamp_finished:
            update_values["finished_at"] = now
        update_values["status"] = target.value
        update_values["updated_at"] = now

        result = self._db.execute(
            update(DailySummaryGenerationAttempt)
            .where(
                DailySummaryGenerationAttempt.id == attempt_id,
                DailySummaryGenerationAttempt.status.in_(source_values),
            )
            .values(**update_values)
        )
        if _rowcount(result) == 0:
            return None
        self._db.flush()
        return self._require_row(attempt_id)

    def _require_row(self, attempt_id: int) -> DailySummaryGenerationAttempt:
        """Re-read ``attempt_id`` with fresh column values.

        ``populate_existing=True`` is load-bearing: the ``mark_*``
        helpers use Core ``UPDATE`` statements, which do not refresh an
        instance already sitting in the session identity map. Without
        it, callers would receive the pre-transition ``status``.
        """
        stmt = (
            select(DailySummaryGenerationAttempt)
            .where(DailySummaryGenerationAttempt.id == attempt_id)
            .execution_options(populate_existing=True)
        )
        return self._db.execute(stmt).scalars().one()


def _active_index_predicate() -> Any:
    """Return the partial-index predicate as a SQL expression.

    Built from the same text constant the model and the migration use
    (:data:`src.models.daily_summary_attempt.ACTIVE_STATUS_PREDICATE`)
    so the ``ON CONFLICT`` target cannot drift away from the index it
    is supposed to match — a mismatch would make PostgreSQL raise
    "there is no unique or exclusion constraint matching the ON
    CONFLICT specification" at runtime.
    """
    return text(ACTIVE_STATUS_PREDICATE)


def _rowcount(result: Any) -> int:
    """Return ``result.rowcount`` as an int, defaulting to ``0``.

    Mypy's stubs do not expose ``rowcount`` on :class:`Result`; the
    runtime always populates it for INSERT / UPDATE / DELETE, so it is
    read through ``getattr`` with a default. Same helper shape as
    :mod:`src.application.outbox.repository`.
    """
    raw = getattr(result, "rowcount", 0) or 0
    return int(raw)


__all__ = [
    "FAILURE_REASON_CANCELLED",
    "FAILURE_REASON_TIMEOUT",
    "DailySummaryAttemptOutcome",
    "DailySummaryAttemptRepository",
]
