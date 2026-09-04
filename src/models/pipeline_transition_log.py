"""SQLAlchemy model for the append-only pipeline transition audit.

Why this table exists
=====================

``src/services/pipeline_state.py`` already enforces the legal transition
sets for ``VideoSession`` and ``TaskLog`` aggregates — every successful
compare-and-set call also embeds a single ``detail_json["transition"]``
record on the ``TaskLog`` row that triggered it. That history is not
enough as the only audit trail for two reasons:

1. ``detail_json`` lives **on** the ``TaskLog`` that drove the
   transition. When the transition happens with no ``TaskLog``
   (the task log can be ``None`` for session transitions during
   sealing / scan completion), the audit metadata has nowhere to
   land. The same is true for ``TaskLog`` transitions themselves —
   the before / after state is recorded on the very row whose
   mutation made the row interesting, so a post-mortem that
   discovers only the final state loses the intermediate history.
2. ``task_log`` is pruned after 7 days by
   ``src.services.maintenance.cleanup_old_task_logs``. Any
   audit data that lives on the ``TaskLog`` row goes with it, which
   defeats the whole point of having an audit trail for the
   pipeline state machine.

This table is the durable, append-only counter-part: every successful
CAS transition writes one row. Rows are never updated, never deleted
in normal operation, and the ``task_log_id`` FK is ``ON DELETE SET
NULL`` so the 7-day ``task_log`` cleanup NULLs the correlation id
instead of cascading the audit row away. The transition history
outlives the run that produced it.

Pipeline state machine
======================

The pipeline has two state machines, both defined in
:mod:`src.services.pipeline_state`:

- ``VideoSession`` rows transition through
  ``OPEN → SEALED → ANALYZING → {SUCCESS, PARTIAL, FAILED, SEALED}``
  (the eleven edges in ``SESSION_ALLOWED_TRANSITIONS``).
- ``TaskLog`` rows transition through
  ``PENDING → {RUNNING, CANCELLED}`` and
  ``RUNNING → {SUCCESS, SKIPPED, FAILED, TIMEOUT, CANCELLED}``
  (the seven edges in ``TASK_LOG_ALLOWED_TRANSITIONS``).

The transition audit row covers **only** these two aggregate types.
Future aggregate types (e.g. ``outbox_event``,
``daily_summary_generation_attempt``) have their own state-machine
audits and do not need a second writer here.

Field contract
==============

====================  ====================  ===================================
Column                Type                  Notes
====================  ====================  ===================================
``id``                BIGSERIAL PK          surrogate; opaque to callers
``aggregate_type``    TEXT(32)              ``"VideoSession"`` / ``"TaskLog"``
``aggregate_id``      BIGINT                PK value of the aggregate row
``from_status``       TEXT(32)              pre-transition status literal
``to_status``         TEXT(32)              post-transition status literal
``reason``            TEXT(128) NULL        short operator / system reason
``source``            TEXT(64)              caller identity (function / module)
``task_log_id``       BIGINT FK → task_log  ``ON DELETE SET NULL``, NULL when
                       NULL                  transition had no driving TaskLog
``occurred_at``       TIMESTAMPTZ           ``NOW()`` default; transition time
``created_at``        TIMESTAMPTZ           inherited from ``Base``
====================  ====================  ===================================

The ``aggregate_type`` / ``aggregate_id`` pair is *not* an SQL-level
foreign key because the two ``aggregate_type`` values point at two
different parent tables (``video_session`` and ``task_log``). The pair
is treated as a logical composite key (indexed with ``occurred_at``
for the audit query path) and a CHECK constraint prevents typo'd
``aggregate_type`` values that point at neither table. The application
contract — :func:`src.application.transition_log.repository.record` —
is the only writer, so it is the only place that has to keep the
typings in sync.

Index plan
==========

1. ``INDEX (aggregate_type, aggregate_id, occurred_at)`` — the
   "replay every transition for aggregate X, oldest first" query that
   the audit / post-mortem code uses to reconstruct the state history.
2. ``INDEX (task_log_id, occurred_at)`` — "every transition triggered
   by TaskLog Y in order" path; the correlation is NULLed by the
   7-day cleanup, so this index shrinks to deletion-friendly
   fragments over time.

The audit is append-only, so there is no UNIQUE constraint on
``(aggregate_type, aggregate_id, occurred_at)``: a single aggregate
can transition multiple times in the same millisecond (e.g.
crash-loop retries within one second), and rejecting later rows
would lose the very transitions the audit exists to capture.

Cross-dialect notes
===================

``BigInteger().with_variant(Integer, "sqlite")`` mirrors the
``daily_summary_generation_attempt`` / ``outbox_event`` pattern:
``BIGSERIAL`` on PostgreSQL, ``INTEGER`` on SQLite so the column is a
rowid alias and autoincrements without a sequence. The SQLite unit
tests therefore exercise the same column contract as production
PostgreSQL — just through an in-memory engine.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base_class import Base

#: The two legal ``aggregate_type`` literals, rendered as a SQL tuple
#: for the CHECK constraint. Kept next to the model so the CHECK text,
#: the migration and the application writer (:func:`record`) cannot
#: drift silently.
AGGREGATE_TYPE_VALUES_SQL = "'VideoSession', 'TaskLog'"


class PipelineTransitionLog(Base):
    """One row per successful ``pipeline_state`` CAS transition.

    Append-only in practice: the repository (``src.application.transition_log.repository``)
    is the sole writer, and it only ever INSERTs. UPDATE / DELETE are
    not part of the contract — the table is the history itself, not a
    cache or a status flag.
    """

    __tablename__ = "pipeline_transition_log"

    # -- surrogate primary key ----------------------------------------
    # ``BIGSERIAL`` on PostgreSQL; ``INTEGER`` on SQLite so the column
    # becomes a rowid alias and autoincrements without a sequence.
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )

    # -- which aggregate the transition belongs to ---------------------
    # ``aggregate_type`` is a string (one of the two values above) rather
    # than an enum so the SQL layer does not need to know about Python
    # enums and so a future aggregate (e.g. ``DailySummaryAttempt``)
    # can be added without rewriting every CHECK. The application-side
    # ``record(...)`` caller is the contract.
    aggregate_type: Mapped[str] = mapped_column(String(32), nullable=False)
    aggregate_id: Mapped[int] = mapped_column(BigInteger, nullable=False)

    # -- the transition itself ----------------------------------------
    # The two status columns are kept as String(32) — wider than the
    # underlying enum values (the longest literal, ``SessionAnalysisStatus``
    # / ``TaskStatus``, fits inside 16 chars) — so a future status
    # rename does not require a column widening. The ``SESSION_ALLOWED_TRANSITIONS``
    # and ``TASK_LOG_ALLOWED_TRANSITIONS`` sets are the canonical
    # validators; this table just records what actually happened.
    from_status: Mapped[str] = mapped_column(String(32), nullable=False)
    to_status: Mapped[str] = mapped_column(String(32), nullable=False)

    # -- operator / system context ------------------------------------
    reason: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    source: Mapped[str] = mapped_column(String(64), nullable=False)

    # -- correlation ---------------------------------------------------
    # Nullable + ``ON DELETE SET NULL``: the transition history must
    # outlive the 7-day ``task_log`` cleanup, so deleting the
    # correlation id is a no-op on the history itself (the audit row
    # keeps ``from_status`` / ``to_status`` / ``reason`` / ``source``).
    task_log_id: Mapped[Optional[int]] = mapped_column(
        BigInteger,
        ForeignKey("task_log.id", ondelete="SET NULL"),
        nullable=True,
    )

    # -- timestamps ----------------------------------------------------
    # ``occurred_at`` is the transition timestamp (defaults to NOW()).
    # ``created_at`` / ``updated_at`` are the inherited Base
    # row-write / row-update timestamps; they usually equal
    # ``occurred_at`` for the same-row insert, but keeping them
    # lets a future backfill stamp a row with an explicit
    # transition time and the inherited write time distinct.
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    # ``created_at`` / ``updated_at`` are inherited from ``Base``.

    __table_args__ = (
        # -- Aggregate-type guard ------------------------------------
        # Only the two pipeline-state-machine aggregate types are
        # valid here. ``outbox_event`` / ``daily_summary_generation_attempt``
        # have their own audit tables and must NOT land in this one,
        # so an unknown ``aggregate_type`` literal is rejected at
        # INSERT time rather than silently polluting the audit
        # stream.
        CheckConstraint(
            f"aggregate_type IN ({AGGREGATE_TYPE_VALUES_SQL})",
            name="ck_pipeline_transition_log_aggregate_type",
        ),
        # -- Audit query path: replay every transition for aggregate X.
        Index(
            "idx_pipeline_transition_log_agg",
            "aggregate_type",
            "aggregate_id",
            "occurred_at",
        ),
        # -- Correlation query: every transition triggered by TaskLog Y.
        Index(
            "idx_pipeline_transition_log_task",
            "task_log_id",
            "occurred_at",
        ),
    )


__all__ = [
    "AGGREGATE_TYPE_VALUES_SQL",
    "PipelineTransitionLog",
]
