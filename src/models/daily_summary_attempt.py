"""SQLAlchemy model for daily-summary generation attempts.

Why this table exists
=====================

Before this table, ``daily_summary`` carried three unrelated
responsibilities at once:

1. the **final, operator-visible result** for a date;
2. the **generation lock** — ``_claim_daily_summary_generation`` in
   ``src/tasks/summarizer.py`` inserted a bare ``DailySummary`` row
   (unique on ``summary_date``) purely to reserve the date before any
   expensive LLM work started;
3. the implicit **failure state** — the cancel / exception paths
   ``DELETE``d that placeholder row, so a failed run left no trace at
   all: no error, no reason, no attempt history.

Conflating those three concerns has two concrete consequences. A
crash between "claim" and "upsert result" leaves a *content-free*
``daily_summary`` row that the API happily serves as an empty daily
report. And a failed / cancelled / timed-out run is indistinguishable
from "was never attempted", so operators cannot answer "did the LLM
fail, or did nobody schedule it?" without digging through
``task_log``.

``daily_summary_generation_attempt`` splits (2) and (3) out into their
own append-only history: **one row per attempt** to generate the
summary for one date. ``DailySummary`` then goes back to being only
(1) — the final visible result — in Todo 19.

Lifecycle
=========

The status column mirrors the shape (not the values) of the outbox
state machine from ADR
``docs/adr/0011-transactional-outbox-and-task-lifecycle.md`` §4:
a short list of states, a frozen transition table, and a CHECK
constraint that keeps unknown values out of the column. The values
themselves are deliberately a **separate** enum from
``OutboxStatus``: an outbox row is about *broker publication*, an
attempt row is about *business generation*, and the two have no
transition in common (an attempt is never ``publishing``; an outbox
row is never ``timed_out``).

::

    claimed ──> queued ──> running ──> succeeded          (happy path)
       │           │          ├──────> failed             (LLM / parse error)
       │           │          ├──────> cancelled          (operator cancel)
       │           ├──────────┼──────> timed_out          (watchdog)
       └───────────┴──────────┴──────> superseded         (newer attempt took over)

The five terminal states (``succeeded`` / ``failed`` / ``cancelled`` /
``timed_out`` / ``superseded``) have **no** outgoing transitions:
a terminal attempt can never be revived. A retry always creates a
*new* row with ``attempt_no = max(attempt_no) + 1`` for that date, so
the history is preserved verbatim.

Field contract
==============

===========================  ===========================  ==================================
Column                       Type                         Notes
===========================  ===========================  ==================================
``id``                       BIGSERIAL PK                 surrogate; opaque to callers
``summary_date``             DATE                         business identity; indexed
``attempt_no``               INT                          per-date monotonic counter, 1-based
``status``                   TEXT                         CHECK; see the 8 values above
``task_log_id``              BIGINT FK NULL               ``ON DELETE SET NULL``
``triggered_by``             TEXT NULL                    ``schedule`` / ``manual`` / ``retry``
``error_type``               TEXT NULL                    exception class name
``last_error``               TEXT NULL                    truncated error message
``failure_reason``           TEXT NULL                    ``llm_error`` / ``cancelled`` /
                                                          ``timeout`` / ``no_events`` / NULL
``started_at``               TIMESTAMPTZ                  ``NOW()`` default
``claimed_at``               TIMESTAMPTZ NULL             when the claim succeeded
``finished_at``              TIMESTAMPTZ NULL             set on every terminal transition
``events_count``             INT NULL                     LLM-input snapshot
``input_token_estimate``     INT NULL                     LLM-input snapshot
``created_at``               TIMESTAMPTZ                  inherited from ``Base``
``updated_at``               TIMESTAMPTZ                  inherited from ``Base``
===========================  ===========================  ==================================

Index / constraint plan
=======================

1. ``PRIMARY KEY (id)`` — surrogate.
2. ``CHECK (status IN (...8 values...))`` — keeps unknown status
   literals out of the column; the repository only ever writes the
   eight legal values, so the CHECK is the structural guard against
   hand-rolled SQL / a future migration typo.
3. **Partial unique** ``(summary_date) WHERE status IN
   ('claimed','queued','running')`` — the *active-attempt safety net*.
   Two concurrent ``INSERT … ON CONFLICT DO NOTHING`` statements for
   the same date cannot both succeed, so "only one attempt is active
   per date" is a schema guarantee rather than an application
   convention. Terminal rows are outside the predicate, so retry
   history accumulates freely.
4. ``INDEX (summary_date, attempt_no)`` — the audit query path
   ("show me every attempt for date X, in order").
5. ``INDEX (summary_date)`` — the ``find_active_for_date`` /
   ``next_attempt_no`` lookup path.
6. ``FOREIGN KEY (task_log_id) REFERENCES task_log(id) ON DELETE SET
   NULL`` — mirrors the existing ``LLMUsageLog`` pattern. ``task_log``
   rows are pruned after 7 days by
   ``src.services.maintenance.cleanup_old_task_logs``; the
   attempt history must **outlive** that cleanup, so the FK nulls the
   correlation id instead of cascading the delete or blocking the
   cleanup with ``RESTRICT``.

Cross-dialect notes
===================

``BigInteger().with_variant(Integer, "sqlite")`` makes ``id`` a rowid
alias on SQLite so unit tests get autoincrement without a sequence.
The partial index is declared with **both** ``postgresql_where`` and
``sqlite_where`` so ``Base.metadata.create_all`` on an in-memory
SQLite engine enforces the same active-attempt uniqueness the
migration emits on PostgreSQL (SQLite has supported partial indexes
since 3.8.0).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base_class import Base

#: The eight legal ``status`` literals, rendered as a SQL tuple for the
#: CHECK constraint. Kept next to the model so the constraint text, the
#: migration and ``DailySummaryAttemptStatus`` cannot drift silently.
STATUS_VALUES_SQL = (
    "'claimed','queued','running','succeeded','failed','cancelled','timed_out','superseded'"
)

#: The three statuses that make an attempt "active" (i.e. currently
#: holding the per-date slot). Used verbatim as the partial-index
#: predicate.
ACTIVE_STATUS_PREDICATE = "status IN ('claimed','queued','running') AND summary_date IS NOT NULL"


class DailySummaryGenerationAttempt(Base):
    """One attempt to generate the daily summary for one date.

    Rows are append-only in practice: the repository never deletes
    them and terminal statuses have no outgoing transitions. A retry
    inserts a new row with the next ``attempt_no`` for the date.
    """

    __tablename__ = "daily_summary_generation_attempt"

    # -- surrogate primary key ----------------------------------------
    # ``BIGSERIAL`` on PostgreSQL; ``INTEGER`` on SQLite so the column
    # becomes a rowid alias and autoincrements without a sequence.
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )

    # -- business identity --------------------------------------------
    summary_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)

    # -- lifecycle state ----------------------------------------------
    # Values: claimed / queued / running / succeeded / failed /
    # cancelled / timed_out / superseded (see module docstring).
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        server_default=text("'claimed'"),
    )

    # -- correlation / provenance -------------------------------------
    # Nullable + ``ON DELETE SET NULL``: attempts outlive the 7-day
    # ``task_log`` cleanup (see module docstring, index plan item 6).
    task_log_id: Mapped[Optional[int]] = mapped_column(
        BigInteger,
        ForeignKey("task_log.id", ondelete="SET NULL"),
        nullable=True,
    )
    triggered_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    # -- operator-facing diagnostics ----------------------------------
    error_type: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    failure_reason: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    # -- bookkeeping ---------------------------------------------------
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    claimed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # -- LLM-input snapshot (post-mortem) ------------------------------
    events_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    input_token_estimate: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # -- ``created_at`` / ``updated_at`` are inherited from Base -------

    __table_args__ = (
        CheckConstraint(
            f"status IN ({STATUS_VALUES_SQL})",
            name="ck_daily_summary_attempt_status",
        ),
        # Active-attempt safety net: at most one claimed/queued/running
        # attempt per date. Declared for both dialects so the SQLite
        # unit tests enforce the same invariant as PostgreSQL.
        Index(
            "uq_daily_summary_attempt_active_date",
            "summary_date",
            unique=True,
            postgresql_where=text(ACTIVE_STATUS_PREDICATE),
            sqlite_where=text(ACTIVE_STATUS_PREDICATE),
        ),
        # Audit query path: "all attempts for date X in order".
        Index(
            "idx_daily_summary_attempt_date_no",
            "summary_date",
            "attempt_no",
        ),
    )


__all__ = [
    "ACTIVE_STATUS_PREDICATE",
    "STATUS_VALUES_SQL",
    "DailySummaryGenerationAttempt",
]
