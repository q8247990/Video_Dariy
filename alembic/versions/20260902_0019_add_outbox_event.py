"""add outbox_event table for transactional task outbox (Wave 3 / Todo 12).

Revision ID: 20260902_0019
Revises: 20260902_0018
Create Date: 2026-09-02 14:00:00.000000

Why this migration exists
-------------------------
This migration creates the ``outbox_event`` table that backs the
transactional outbox designed in ADR
``docs/adr/0011-transactional-outbox-and-task-lifecycle.md``. The
field list, the index / constraint plan, and the state machine are
all frozen by the contract layer (Todo 11); this migration is the
PostgreSQL realization of that contract.

Two-table responsibility split
------------------------------
The plan is explicit (ADR §1, "Outbox / TaskLog responsibilities
split"): ``task_log`` keeps owning the business run lifecycle; this
new ``outbox_event`` table owns broker publication. They are linked
1:1 by ``outbox_event.task_log_id`` (FK ``ON DELETE RESTRICT``),
``event_id`` is reused as the Celery ``task_id``, and the consumer
short-circuits duplicate publishes via the existing
``bind_or_create_running_task_log`` helper.

Field contract (ADR §2, "Field contract (canonical list)")
---------------------------------------------------------
17 columns: ``id`` BIGSERIAL PK, ``event_id`` UUID (global UNIQUE),
``task_log_id`` BIGINT FK → ``task_log(id)`` (UNIQUE),
``dedupe_key`` TEXT NULL, ``task_name`` TEXT, ``queue`` TEXT,
``args_json`` JSONB, ``kwargs_json`` JSONB, ``status`` TEXT (CHECK
IN ``pending/publishing/published/failed``), ``attempt_count`` INT,
``next_attempt_at`` TIMESTAMPTZ, ``claimed_by`` TEXT NULL,
``lease_expires_at`` TIMESTAMPTZ NULL, ``published_at`` TIMESTAMPTZ
NULL, ``last_error`` TEXT NULL, ``created_at`` TIMESTAMPTZ,
``updated_at`` TIMESTAMPTZ.

Index / constraint plan (ADR §2)
--------------------------------
1. ``PRIMARY KEY (id)`` surrogate.
2. ``UNIQUE (event_id)`` global idempotency.
3. ``UNIQUE (task_log_id)`` one outbox row per business run.
4. Partial unique ``(task_log_id) WHERE status='pending'`` —
   concurrency safety net; the second concurrent ``INSERT … ON
   CONFLICT DO NOTHING`` returns no row.
5. Composite ``(status, next_attempt_at)`` publisher hot path.
6. Partial ``(status) WHERE status='failed'`` failure-scan (Todo 24).
7. Partial ``(status, published_at) WHERE status='published'``
   retention cleanup hot path.
8. ``FOREIGN KEY (task_log_id) REFERENCES task_log(id) ON DELETE
   RESTRICT`` — the outbox MUST NOT outlive its run silently.

Schema preflight (in ``upgrade()``)
-----------------------------------
The migration asserts ``task_log`` exists before creating the
``outbox_event`` table. The FK target is mandatory; a database that
has somehow lost ``task_log`` (out-of-band cleanup, half-applied
migration) must NOT silently no-op this revision — that would leave
a dangling table reference and an inconsistent schema. The check
uses ``information_schema.tables`` so it is schema-aware and
survives tablespace / role quirks.

Irreversibility
---------------
``downgrade()`` raises ``NotImplementedError`` rather than attempting
to recreate the dropped table. Operators wanting to roll back MUST
restore the pre-upgrade database backup that AGENTS.md §10 already
requires for irreversible migrations; the cutover (Todo 14) and the
operations runbook (Todo 24) will not function correctly if the
outbox table is missing.

Why forward-only
---------------
The outbox is the contract surface for the publisher (Todo 13) and
the cutover (Todo 14); dropping the partial unique index on
``(task_log_id) WHERE status='pending'`` would silently break the
concurrency guarantee that ADR §2 item 4 is the structural safety
net for. A downgrade would have to re-create the index, the
constraints and the foreign key with no data to validate against
(historical TaskLog rows are NOT back-promoted — see ADR
"Migration risk"); the only safe path is the verified DB backup.

This migration is irreversible in the same sense as the prior
``20260825_0012``, ``20260825_0014``, ``20260826_0017``, and
``20260902_0018`` revisions.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260902_0019"
down_revision: Union[str, None] = "20260902_0018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _assert_task_log_table_present() -> None:
    """Abort when the ``task_log`` table the FK points at is missing.

    The outbox table is meaningless without ``task_log``: the FK is
    ``NOT NULL`` on ``task_log_id`` and ``UNIQUE`` to enforce the
    one-outbox-row-per-run invariant. A database that has somehow lost
    ``task_log`` (out-of-band cleanup, half-applied migration) must
    not silently accept this revision; that would leave a dangling
    table reference and an inconsistent schema. Refuse and let the
    operator investigate before issuing ``alembic stamp head`` or
    re-running.
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
            "outbox_event migration preflight failed: "
            "task_log table is missing from the current schema. "
            "The outbox FK (outbox_event.task_log_id -> task_log.id) "
            "cannot be created without it. Investigate before "
            "proceeding; do not stamp this revision."
        )


def upgrade() -> None:
    _assert_task_log_table_present()

    op.create_table(
        "outbox_event",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_log_id", sa.BigInteger(), nullable=False),
        sa.Column("dedupe_key", sa.Text(), nullable=True),
        sa.Column("task_name", sa.Text(), nullable=False),
        sa.Column("queue", sa.Text(), nullable=False),
        sa.Column(
            "args_json",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=False,
        ),
        sa.Column(
            "kwargs_json",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column(
            "attempt_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("claimed_by", sa.Text(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name="pk_outbox_event"),
        sa.UniqueConstraint("event_id", name="uq_outbox_event_event_id"),
        sa.UniqueConstraint("task_log_id", name="uq_outbox_event_task_log_id"),
        sa.CheckConstraint(
            "status IN ('pending','publishing','published','failed')",
            name="ck_outbox_event_status",
        ),
        sa.ForeignKeyConstraint(
            ["task_log_id"],
            ["task_log.id"],
            name="fk_outbox_event_task_log",
            ondelete="RESTRICT",
        ),
    )

    # -- Concurrency safety net (ADR §2 item 4) -----------------------
    # Partial unique on ``task_log_id`` while the row is pending.
    # The ``task_log_id IS NOT NULL`` predicate is redundant (the
    # column is NOT NULL) but is frozen verbatim by ADR §2 item 4 so
    # the migration and the persistence model stay byte-identical to
    # the contract.
    op.create_index(
        "uq_outbox_event_pending_task_log",
        "outbox_event",
        ["task_log_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending' AND task_log_id IS NOT NULL"),
    )

    # -- Publisher hot path (ADR §2 item 5) ---------------------------
    op.create_index(
        "idx_outbox_event_status_next_attempt",
        "outbox_event",
        ["status", "next_attempt_at"],
    )

    # -- Failure-scan index (Todo 24 dashboard, ADR §2 item 6) ------
    op.create_index(
        "idx_outbox_event_failed",
        "outbox_event",
        ["status"],
        postgresql_where=sa.text("status = 'failed'"),
    )

    # -- Retention cleanup hot path (ADR §2 item 7) ------------------
    op.create_index(
        "idx_outbox_event_published",
        "outbox_event",
        ["status", "published_at"],
        postgresql_where=sa.text("status = 'published'"),
    )


def downgrade() -> None:
    raise NotImplementedError("irreversible migration - restore from verified DB backup")
