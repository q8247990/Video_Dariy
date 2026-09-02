"""add daily_summary_generation_attempt table (Wave 4 / Todo 15).

Revision ID: 20260902_0020
Revises: 20260902_0019
Create Date: 2026-09-02 18:00:00.000000

Why this migration exists
-------------------------
``daily_summary`` used to carry three unrelated responsibilities: the
final operator-visible result for a date, the *generation lock*
(``_claim_daily_summary_generation`` inserted a bare row purely to
reserve ``summary_date`` before any LLM work), and the implicit failure
state (the cancel / exception paths ``DELETE``d that placeholder, so a
failed run left no trace at all).

Two concrete consequences follow from that conflation. A crash between
"claim" and "upsert result" leaves a content-free ``daily_summary`` row
that the API serves as an empty daily report. And a failed / cancelled
/ timed-out run is indistinguishable from "never attempted", so
operators cannot tell an LLM failure from a missing schedule without
digging through ``task_log`` — which is pruned after 7 days.

This migration creates ``daily_summary_generation_attempt``: an
append-only history with **one row per attempt** for one date. It owns
the lock (via a partial unique index) and the failure diagnostics.
``daily_summary`` goes back to being only the final visible result in
Todo 19; this revision is purely additive and touches no existing table.

Field contract
--------------
16 columns: ``id`` BIGSERIAL PK, ``summary_date`` DATE,
``attempt_no`` INT, ``status`` TEXT (CHECK IN the eight lifecycle
values), ``task_log_id`` BIGINT FK → ``task_log(id)`` NULL,
``triggered_by`` TEXT NULL, ``error_type`` TEXT NULL,
``last_error`` TEXT NULL, ``failure_reason`` TEXT NULL,
``started_at`` TIMESTAMPTZ, ``claimed_at`` TIMESTAMPTZ NULL,
``finished_at`` TIMESTAMPTZ NULL, ``events_count`` INT NULL,
``input_token_estimate`` INT NULL, ``created_at`` TIMESTAMPTZ,
``updated_at`` TIMESTAMPTZ.

Index / constraint plan
-----------------------
1. ``PRIMARY KEY (id)`` surrogate.
2. ``CHECK (status IN ('claimed','queued','running','succeeded',
   'failed','cancelled','timed_out','superseded'))`` — the repository
   only ever writes those eight literals, so the CHECK is the
   structural guard against hand-rolled SQL and future typos.
3. Partial unique ``(summary_date) WHERE status IN
   ('claimed','queued','running')`` — the **active-attempt safety
   net**. Two concurrent ``INSERT … ON CONFLICT DO NOTHING`` statements
   for the same date cannot both succeed, so "at most one attempt is
   active per date" is a schema guarantee rather than an application
   convention. Terminal rows fall outside the predicate, so retry
   history accumulates freely.
4. ``INDEX (summary_date, attempt_no)`` — the audit query path
   ("every attempt for date X, in order").
5. ``INDEX (summary_date)`` — the ``find_active_for_date`` /
   ``next_attempt_no`` lookup path.
6. ``FOREIGN KEY (task_log_id) REFERENCES task_log(id) ON DELETE SET
   NULL`` — mirrors the existing ``llm_usage_log`` pattern. TaskLog
   rows are pruned after 7 days by
   ``src/tasks/task_maintenance.py:_cleanup_old_task_logs``; the
   attempt history must **outlive** that cleanup, so the FK nulls the
   correlation id rather than cascading the delete (which would erase
   the diagnostics) or blocking with ``RESTRICT`` (which would wedge
   the cleanup task).

Schema preflight (in ``upgrade()``)
-----------------------------------
The migration asserts ``task_log`` exists before creating the table:
the FK target is mandatory, and a database that has somehow lost
``task_log`` (out-of-band cleanup, half-applied migration) must NOT
silently no-op this revision — that would leave an inconsistent schema
whose missing FK only surfaces much later. The check uses
``information_schema.tables`` so it is schema-aware and survives
tablespace / role quirks. Same shape as
``20260902_0019_add_outbox_event.py``.

Irreversibility
---------------
``downgrade()`` raises ``NotImplementedError`` rather than dropping the
table. The attempt history is the only durable record of *why* a daily
summary failed; a downgrade would silently destroy the operator audit
trail, and re-creating the partial unique index afterwards has no data
to validate against. Operators wanting to roll back MUST restore the
pre-upgrade database backup that AGENTS.md §10 already requires for
irreversible migrations.

This migration is irreversible in the same sense as the prior
``20260825_0012``, ``20260825_0014``, ``20260826_0017``,
``20260902_0018``, and ``20260902_0019`` revisions.
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "20260902_0020"
down_revision: Union[str, None] = "20260902_0019"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

STATUS_CHECK_SQL = (
    "status IN ('claimed','queued','running','succeeded',"
    "'failed','cancelled','timed_out','superseded')"
)

ACTIVE_INDEX_PREDICATE_SQL = "status IN ('claimed','queued','running') AND summary_date IS NOT NULL"


def _assert_task_log_table_present() -> None:
    """Abort when the ``task_log`` table the FK points at is missing.

    The attempt table correlates each run with ``task_log`` so operators
    can jump from "this attempt failed" to the task's logs. A database
    that has somehow lost ``task_log`` (out-of-band cleanup,
    half-applied migration) must not silently accept this revision;
    that would create the table without its foreign key and leave the
    schema inconsistent with the model. Refuse and let the operator
    investigate before issuing ``alembic stamp head`` or re-running.
    """

    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = current_schema() "
            "AND table_name = 'task_log'"
        )
    ).fetchall()
    if not rows:
        raise RuntimeError(
            "daily_summary_generation_attempt migration preflight failed: "
            "task_log table is missing from the current schema. "
            "The attempt FK (daily_summary_generation_attempt.task_log_id "
            "-> task_log.id) cannot be created without it. Investigate "
            "before proceeding; do not stamp this revision."
        )


def upgrade() -> None:
    _assert_task_log_table_present()

    op.create_table(
        "daily_summary_generation_attempt",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("summary_date", sa.Date(), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'claimed'"),
        ),
        sa.Column("task_log_id", sa.BigInteger(), nullable=True),
        sa.Column("triggered_by", sa.String(length=64), nullable=True),
        sa.Column("error_type", sa.String(length=128), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("failure_reason", sa.String(length=64), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("events_count", sa.Integer(), nullable=True),
        sa.Column("input_token_estimate", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_daily_summary_generation_attempt"),
    )

    # -- Active-attempt safety net (index plan item 3) ----------------
    # At most one claimed/queued/running attempt per date. The
    # ``summary_date IS NOT NULL`` predicate is redundant (the column is
    # NOT NULL) but is kept byte-identical to the model's
    # ``ACTIVE_STATUS_PREDICATE`` so the repository's ``ON CONFLICT …
    # WHERE`` target matches this index exactly; a mismatch makes
    # PostgreSQL reject the INSERT at runtime.
    op.create_index(
        "uq_daily_summary_attempt_active_date",
        "daily_summary_generation_attempt",
        ["summary_date"],
        unique=True,
        postgresql_where=sa.text(ACTIVE_INDEX_PREDICATE_SQL),
    )

    # -- Audit query path (index plan item 4) -------------------------
    op.create_index(
        "idx_daily_summary_attempt_date_no",
        "daily_summary_generation_attempt",
        ["summary_date", "attempt_no"],
    )

    # -- Lookup path for find_active_for_date / next_attempt_no ------
    op.create_index(
        "ix_daily_summary_generation_attempt_summary_date",
        "daily_summary_generation_attempt",
        ["summary_date"],
    )

    op.create_foreign_key(
        "fk_daily_summary_attempt_task_log",
        "daily_summary_generation_attempt",
        "task_log",
        ["task_log_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.create_check_constraint(
        "ck_daily_summary_attempt_status",
        "daily_summary_generation_attempt",
        STATUS_CHECK_SQL,
    )


def downgrade() -> None:
    raise NotImplementedError("irreversible migration - restore from verified DB backup")
