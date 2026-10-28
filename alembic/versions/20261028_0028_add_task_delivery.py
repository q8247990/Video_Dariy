"""add task_delivery dispatch intent table

Revision ID: 20261028_0028
Revises: 20260928_0027
Create Date: 2026-10-28 00:00:00.000000

Why
---
ADR 0013 replaces the standalone outbox publisher with a pre-assigned
``task_id`` plus a per-row delivery ledger. The dispatcher writes one
``task_delivery`` row per ``TaskLog`` in the caller's transaction; the
caller flushes it to the broker after commit and the heartbeat re-sends
rows whose last attempt failed. The ledger keeps the per-attempt count
and last error that operators used to read off ``outbox_event``.

``outbox_event`` is intentionally left in place: its migration is
irreversible, so dropping the table would strand any deployment that has
not taken a verified backup (ADR 0012 §5). The retired publisher leaves
the rows ``pending`` and they are replayed once by the first flush cycle
(:func:`src.services.dispatch.outbox.replay_retired_outbox`).

Reversibility
-------------
``downgrade()`` drops ``task_delivery``. Rows are operational scratch data
(one per in-flight dispatch), so nothing of audit value is lost.
"""

from typing import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20261028_0028"
down_revision: str | None = "20260928_0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "task_delivery"


def upgrade() -> None:
    bind = op.get_bind()
    exists = bind.execute(
        sa.text(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = current_schema() AND table_name = :table"
        ),
        {"table": TABLE},
    ).fetchone()
    if exists is not None:
        return
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "task_log_id",
            sa.Integer(),
            sa.ForeignKey("task_log.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("task_name", sa.String(length=255), nullable=False),
        sa.Column("queue", sa.String(length=64), nullable=False),
        sa.Column("args_json", sa.JSON(), nullable=False),
        sa.Column("kwargs_json", sa.JSON(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(length=1024), nullable=True),
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
        sa.UniqueConstraint("task_log_id", name="uq_task_delivery_task_log_id"),
    )


def downgrade() -> None:
    op.drop_table(TABLE)
