"""drop llm_provider video_preprocess / keyframe columns (contract cleanup).

Revision ID: 20260902_0018
Revises: 20260826_0017
Create Date: 2026-09-02 12:00:00.000000

Why this migration exists
-------------------------
The keyframe preprocessing pipeline was retired as a product decision
(ADR ``0009-remove-keyframe-pipeline.md``). Tod 9 (commit 852338d) removed
the disabled code path; tod 10 finishes the cleanup by retiring the
``video_preprocess_mode``, ``video_keyframe_target_n`` and
``video_keyframe_jpeg_quality`` columns from the ``llm_provider`` table,
the ``LLMProvider`` model, the ``LLMProviderBase`` schema, and the
frontend ``Provider`` type.

This is an irreversible contract migration. The columns are dropped so
that no row, view, or future write can ever re-introduce the
configuration surface; downgrade is intentionally rejected.

Pre-upgrade requirements
------------------------
The migration MUST NOT run on a database that has not been verified
against the deployment runbook:

1. A verified PostgreSQL backup must be taken before invoking
   ``alembic upgrade head``. The only recovery path after a failed or
   partially applied run is restoring that backup. See
   ``README.md`` -> "迁移前必须先做已验证的数据库备份" for the
   operational checklist.
2. All Celery workers, the backend API process, the MCP entry point,
   and any out-of-process clients must be running a build that already
   drops these fields from the in-memory schema/model. Running this
   migration against a stale worker would silently discard the column
   before clients stop writing to it, breaking compatibility without
   surfacing a clear error. The contract assumption is that tod 9's code
   removal has shipped everywhere.

Schema preflight (in ``upgrade()``)
-----------------------------------
The three columns must all exist on ``llm_provider`` before this
migration runs. If any of them is already absent (a half-applied or
already-cleaned database), the migration aborts with ``RuntimeError``
rather than silently no-op'ing, so the operator is forced to
investigate. The check uses ``information_schema.columns`` so it is
schema-aware and survives tablespace/role quirks.

Irreversibility
---------------
``downgrade()`` raises ``NotImplementedError`` rather than attempting
to recreate the columns. Restoring the data is impossible without the
verified backup mentioned above: column defaults (``'raw_mp4'``,
``120``, ``88``) are reproducible, but every per-row value chosen
before the migration is not. Operators who need to roll back must
restore the pre-upgrade backup instead.
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "20260902_0018"
down_revision: Union[str, None] = "20260826_0017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

DROPPED_COLUMNS: tuple[str, ...] = (
    "video_preprocess_mode",
    "video_keyframe_target_n",
    "video_keyframe_jpeg_quality",
)


def _assert_columns_present() -> None:
    """Abort when any of the three retired columns is already missing.

    The migration assumes the contract pre-state (all three columns
    present). A database that has already lost them must not be
    silently accepted: that would imply either a half-applied previous
    run, an out-of-band operator cleanup, or a deployment that does not
    match the contract. In all three cases we refuse to run so the
    operator investigates before issuing ``alembic stamp head`` or
    re-running.
    """

    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() "
            "AND table_name = 'llm_provider' "
            "AND column_name = ANY(:columns)"
        ),
        {"columns": list(DROPPED_COLUMNS)},
    ).fetchall()
    present = {row[0] for row in rows}
    missing = [name for name in DROPPED_COLUMNS if name not in present]
    if missing:
        joined = ", ".join(missing)
        raise RuntimeError(
            "keyframe contract migration preflight failed: "
            f"llm_provider missing expected column(s): {joined}. "
            "Either the database is already past 20260902_0018 or an "
            "out-of-band schema change has dropped them. Investigate "
            "before proceeding; do not stamp this revision."
        )


def upgrade() -> None:
    _assert_columns_present()
    for column in DROPPED_COLUMNS:
        op.drop_column("llm_provider", column)


def downgrade() -> None:
    raise NotImplementedError(
        "irreversible migration - restore from verified DB backup"
    )
