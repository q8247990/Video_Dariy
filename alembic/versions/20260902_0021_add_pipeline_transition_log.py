"""add pipeline_transition_log table (Wave 4 / Todo 17).

Revision ID: 20260902_0021
Revises: 20260902_0020
Create Date: 2026-09-02 20:00:00.000000

Why this migration exists
-------------------------

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
   ``src/tasks/task_maintenance.py:_cleanup_old_task_logs``. Any
   audit data that lives on the ``TaskLog`` row goes with it, which
   defeats the whole point of having an audit trail for the
   pipeline state machine.

This migration creates ``pipeline_transition_log``: an append-only
history with **one row per successful CAS transition** for one of
the two pipeline aggregates (currently ``VideoSession`` and
``TaskLog``). Rows are never updated in normal operation; the
``task_log_id`` FK is ``ON DELETE SET NULL`` so the 7-day
``task_log`` cleanup NULLs the correlation id rather than cascading
the audit row away (which would erase the diagnostics) or blocking
the cleanup with ``RESTRICT`` (which would wedge the cleanup task).

Field contract
--------------
12 columns: ``id`` BIGSERIAL PK, ``aggregate_type`` TEXT(32),
``aggregate_id`` BIGINT, ``from_status`` TEXT(32), ``to_status``
TEXT(32), ``reason`` TEXT(128) NULL, ``source`` TEXT(64),
``task_log_id`` BIGINT FK → ``task_log(id)`` NULL
(``ON DELETE SET NULL``), ``occurred_at`` TIMESTAMPTZ,
``created_at`` TIMESTAMPTZ, ``updated_at`` TIMESTAMPTZ.

The two ``aggregate_type`` values point at two different parent
tables (``video_session`` and ``task_log``), so the
``(aggregate_type, aggregate_id)`` pair is a logical composite key
rather than an SQL FK. A CHECK constraint pins the two legal
``aggregate_type`` literals so a typo'd aggregate type — or a
future transition-log row from a *different* aggregate kind — is
rejected at INSERT time rather than silently polluting the audit
stream. Future aggregate types (e.g. ``DailySummaryAttempt``) have
their own audit tables and must NOT land in this one.

Index / constraint plan
-----------------------
1. ``PRIMARY KEY (id)`` surrogate.
2. ``CHECK (aggregate_type IN ('VideoSession', 'TaskLog'))`` —
   the structural guard against hand-rolled SQL and future typos.
   The repository only ever writes those two literals, so the CHECK
   is the same shape as the ``daily_summary_generation_attempt``
   status CHECK (``20260902_0020`` item 2).
3. ``INDEX (aggregate_type, aggregate_id, occurred_at)`` — the
   "replay every transition for aggregate X, oldest first" query
   path that the audit / post-mortem code uses to reconstruct the
   state history.
4. ``INDEX (task_log_id, occurred_at)`` — "every transition
   triggered by TaskLog Y in order" path; the correlation is NULLed
   by the 7-day cleanup, so this index shrinks over time without
   blocking the cleanup task.
5. ``FOREIGN KEY (task_log_id) REFERENCES task_log(id) ON DELETE
   SET NULL`` — mirrors the existing ``llm_usage_log`` and
   ``daily_summary_generation_attempt`` pattern. ``TaskLog`` rows
   are pruned after 7 days; the transition history must **outlive**
   that cleanup so post-mortems a month later can still tell "what
   state was the aggregate in when the task_log vanished?".

The audit is append-only and a single aggregate can legitimately
transition multiple times in the same millisecond (crash-loop
retries, double-tap UI). No UNIQUE constraint is declared on
``(aggregate_type, aggregate_id, occurred_at)``: rejecting later
rows would lose the very transitions the audit exists to capture.

Schema preflight (in ``upgrade()``)
-----------------------------------
The migration asserts ``task_log`` exists before creating the table.
The FK target is mandatory, and a database that has somehow lost
``task_log`` (out-of-band cleanup, half-applied migration) must NOT
silently no-op this revision — that would leave an inconsistent
schema whose missing FK only surfaces much later. The check uses
``information_schema.tables`` so it is schema-aware and survives
tablespace / role quirks. Same shape as ``20260902_0020`` (the prior
revision this one extends).

Irreversibility
---------------
``downgrade()`` raises ``NotImplementedError`` rather than dropping
the table. The transition history is the only durable record of
*every* state-machine change the pipeline has performed since its
inception — a downgrade would silently destroy the operator audit
trail, and re-creating the indexes has no data to validate against.
Operators wanting to roll back MUST restore the pre-upgrade database
backup that AGENTS.md §10 already requires for irreversible
migrations.

This migration is irreversible in the same sense as the prior
``20260825_0012``, ``20260825_0014``, ``20260826_0017``,
``20260902_0018``, ``20260902_0019``, and ``20260902_0020``
revisions.
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "20260902_0021"
down_revision: Union[str, None] = "20260902_0020"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

AGGREGATE_TYPE_CHECK_SQL = "aggregate_type IN ('VideoSession', 'TaskLog')"


def _assert_task_log_table_present() -> None:
    """Abort when the ``task_log`` table the FK points at is missing.

    The transition log correlates each CAS event with the ``TaskLog``
    that drove it (when one exists) so a post-mortem can pivot from
    "this aggregate moved" to "because of this task". A database that
    has somehow lost ``task_log`` (out-of-band cleanup, half-applied
    migration) must not silently accept this revision; that would
    leave the table without its only FK and an inconsistent schema.
    Refuse and let the operator investigate before issuing
    ``alembic stamp head`` or re-running.
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
            "pipeline_transition_log migration preflight failed: "
            "task_log table is missing from the current schema. "
            "The transition FK (pipeline_transition_log.task_log_id "
            "-> task_log.id) cannot be created without it. "
            "Investigate before proceeding; do not stamp this revision."
        )


def upgrade() -> None:
    _assert_task_log_table_present()

    op.create_table(
        "pipeline_transition_log",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("aggregate_type", sa.String(length=32), nullable=False),
        sa.Column("aggregate_id", sa.BigInteger(), nullable=False),
        sa.Column("from_status", sa.String(length=32), nullable=False),
        sa.Column("to_status", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.String(length=128), nullable=True),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("task_log_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
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
        sa.PrimaryKeyConstraint("id", name="pk_pipeline_transition_log"),
    )

    # -- Aggregate-type guard (index plan item 2) ---------------------
    # Only the two pipeline-state-machine aggregate types are valid
    # here. ``outbox_event`` / ``daily_summary_generation_attempt``
    # already have their own audit tables and must NOT land in this
    # one, so the structural CHECK prevents drift between the
    # application writer (:func:`src.application.transition_log.repository.record`)
    # and the data.
    op.create_check_constraint(
        "ck_pipeline_transition_log_aggregate_type",
        "pipeline_transition_log",
        AGGREGATE_TYPE_CHECK_SQL,
    )

    # -- Audit query path (index plan item 3) -------------------------
    # "Replay every transition for aggregate X, oldest first" — the
    # typical audit / post-mortem query.
    op.create_index(
        "idx_pipeline_transition_log_agg",
        "pipeline_transition_log",
        ["aggregate_type", "aggregate_id", "occurred_at"],
    )

    # -- Correlation path (index plan item 4) -------------------------
    # "Every transition triggered by TaskLog Y, oldest first". FK is
    # ``ON DELETE SET NULL``; the index shrinks over time without
    # blocking the 7-day ``task_log`` cleanup.
    op.create_index(
        "idx_pipeline_transition_log_task",
        "pipeline_transition_log",
        ["task_log_id", "occurred_at"],
    )

    # -- Correlation FK (index plan item 5) ---------------------------
    # Mirrors ``llm_usage_log`` and ``daily_summary_generation_attempt``:
    # the transition history must outlive the ``task_log`` cleanup
    # so ``SET NULL`` is the only safe choice (``CASCADE`` would
    # erase the audit, ``RESTRICT`` would wedge the cleanup task).
    op.create_foreign_key(
        "fk_pipeline_transition_log_task_log",
        "pipeline_transition_log",
        "task_log",
        ["task_log_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    raise NotImplementedError("irreversible migration - restore from verified DB backup")
