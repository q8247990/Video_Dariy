"""outbox_event.task_log_id FK RESTRICT -> SET NULL + nullable (Issue 3).

Revision ID: 20261124_0029
Revises: 20261028_0028
Create Date: 2026-11-24 10:00:00.000000

Why this migration exists
-------------------------
``cleanup_old_task_logs`` bulk-deletes terminal ``task_log`` rows, but
the ``outbox_event`` FK was declared ``ON DELETE RESTRICT`` (original
ADR §2 item 8). Every hourly heartbeat therefore raised
``ForeignKeyViolation`` inside the retention branch, which permanently
killed hourly retention AND the ``mark_missing_video_files`` sweep that
runs after it in the same branch. Production accumulated published
outbox rows that can never be deleted.

The fix aligns ``outbox_event`` with ``pipeline_transition_log`` and
``daily_summary_generation_attempt``, whose ``task_log_id`` FKs are
already ``ON DELETE SET NULL``: the durable audit trail survives the
per-task lifecycle row instead of blocking its cleanup.

Changes
-------
1. Drop ``fk_outbox_event_task_log`` (RESTRICT).
2. ``task_log_id`` becomes NULLABLE.
3. Recreate ``fk_outbox_event_task_log`` with ``ON DELETE SET NULL``.

The ``UNIQUE (task_log_id)`` constraint is kept unchanged: PostgreSQL
treats NULLs as distinct, so multiple NULL-orphaned rows coexist. The
partial unique ``uq_outbox_event_pending_task_log`` already carries the
``task_log_id IS NOT NULL`` predicate, so NULL rows never collide there
either.

Reversibility
-------------
``downgrade()`` first deletes ``task_log_id IS NULL`` rows (they have
no task_log to point at; a NOT NULL FK cannot be restored while they
exist) and then restores NOT NULL + the RESTRICT FK. Deleting only
NULL-orphaned rows is safe for a fresh downgrade of a schema where the
retention sweep has not yet orphaned anything.
"""

from typing import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20261124_0029"
down_revision: str | None = "20261028_0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _restore_fk(ondelete: str) -> None:
    op.create_foreign_key(
        "fk_outbox_event_task_log",
        "outbox_event",
        "task_log",
        ["task_log_id"],
        ["id"],
        ondelete=ondelete,
    )


def upgrade() -> None:
    op.drop_constraint("fk_outbox_event_task_log", "outbox_event", type_="foreignkey")
    op.alter_column("outbox_event", "task_log_id", existing_type=sa.BigInteger(), nullable=True)
    _restore_fk("SET NULL")


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text("DELETE FROM outbox_event WHERE task_log_id IS NULL"))
    op.drop_constraint("fk_outbox_event_task_log", "outbox_event", type_="foreignkey")
    op.alter_column("outbox_event", "task_log_id", existing_type=sa.BigInteger(), nullable=False)
    _restore_fk("RESTRICT")
