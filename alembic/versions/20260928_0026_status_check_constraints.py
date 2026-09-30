"""add DB CHECK constraints for video_session.analysis_status and task_log.status

Revision ID: 20260928_0026
Revises: 20260923_0025
Create Date: 2026-09-28 00:00:00.000000

Why
---
``VideoSession.analysis_status`` and ``TaskLog.status`` were only guarded by
the application-level state machine in ``src/services/pipeline_state.py``.
Nothing at the database level prevented an illegal value from being written,
and the ``VideoSession`` Python default had drifted to ``"pending"`` — a state
that does not exist in ``SessionAnalysisStatus`` (open / sealed / analyzing /
partial / success / failed). This revision:

1. normalises the legacy ``analysis_status = 'pending'`` rows to ``'open'``
   (``open`` is the only pre-analysis state; ``pending`` was only ever the
   stale column default and was never produced by the pipeline itself);
2. refuses to run when any *other* out-of-contract value is present, so the
   operator investigates instead of having data silently rewritten;
3. adds ``ck_video_session_analysis_status`` and ``ck_task_log_status``.

Reversibility
-------------
``downgrade()`` drops both CHECK constraints, so the schema change is
reversible. The ``'pending' -> 'open'`` normalisation of step 1 is a data
migration and is **not** undone by ``downgrade()`` (the distinction between a
stale default and a genuine ``open`` row is not recoverable; both mean
"not yet analysed").
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "20260928_0026"
down_revision: Union[str, None] = "20260923_0025"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SESSION_STATUSES: tuple[str, ...] = (
    "open",
    "sealed",
    "analyzing",
    "partial",
    "success",
    "failed",
)
TASK_STATUSES: tuple[str, ...] = (
    "pending",
    "running",
    "success",
    "skipped",
    "failed",
    "timeout",
    "cancelled",
)

SESSION_CONSTRAINT = "ck_video_session_analysis_status"
TASK_CONSTRAINT = "ck_task_log_status"


def _assert_no_unexpected_values(table: str, column: str, allowed: tuple[str, ...]) -> None:
    """Abort when ``table.column`` holds a value outside ``allowed``."""
    allowed_sql = ", ".join(f"'{value}'" for value in allowed)
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            f"SELECT DISTINCT {column} FROM {table} "
            f"WHERE {column} IS NOT NULL AND {column} NOT IN ({allowed_sql})"
        )
    ).fetchall()
    if rows:
        offenders = sorted(str(row[0]) for row in rows)
        raise RuntimeError(
            f"{table}.{column} contains values outside the state machine: {offenders}. "
            "Normalise them before running this migration; do not stamp it."
        )


def upgrade() -> None:
    op.execute(
        "UPDATE video_session SET analysis_status = 'open' WHERE analysis_status = 'pending'"
    )
    _assert_no_unexpected_values("video_session", "analysis_status", SESSION_STATUSES)
    _assert_no_unexpected_values("task_log", "status", TASK_STATUSES)

    session_list = ", ".join(f"'{value}'" for value in SESSION_STATUSES)
    task_list = ", ".join(f"'{value}'" for value in TASK_STATUSES)
    op.create_check_constraint(
        SESSION_CONSTRAINT, "video_session", f"analysis_status IN ({session_list})"
    )
    op.create_check_constraint(TASK_CONSTRAINT, "task_log", f"status IN ({task_list})")


def downgrade() -> None:
    op.drop_constraint(SESSION_CONSTRAINT, "video_session", type_="check")
    op.drop_constraint(TASK_CONSTRAINT, "task_log", type_="check")
