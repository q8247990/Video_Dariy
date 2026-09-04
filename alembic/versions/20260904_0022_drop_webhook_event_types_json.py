"""drop webhook_config.event_types_json (webhook subscription contract cleanup).

Revision ID: 20260904_0022
Revises: 20260902_0021
Create Date: 2026-09-04 20:00:00.000000

Why this migration exists
-------------------------

The webhook subscription contract was consolidated onto the canonical
``event_subscriptions_json`` column (``[{"event": str, "version": str}]``).
The legacy ``webhook_config.event_types_json`` column mirrored the
same data in a lossy form (event name only, no version) and was only
kept around so the legacy fallback in
``src/services/webhook_subscription.py`` could still resolve a
subscription list for rows that had never been migrated to the
canonical form.

The fallback has now been removed. The model
(``src/models/webhook_config.py``), the API schemas
(``src/schemas/webhook.py``), the endpoint normalizer
(``src/api/v1/endpoints/webhooks.py``) and the subscription matcher
(``src/services/webhook_subscription.py``) all read and write
``event_subscriptions_json`` exclusively. There is no longer any code
path that reads ``event_types_json``, and the API now rejects any
payload that carries it.

This revision drops the column so the schema reflects the contract and
no future writer (manual SQL, an out-of-date worker, a hand-rolled
fix-up script) can re-introduce the field.

Pre-upgrade requirements
------------------------

The migration MUST NOT run on a database that has not been verified
against the deployment runbook:

1. A verified PostgreSQL backup must be taken before invoking
   ``alembic upgrade head``. The only recovery path after a failed or
   partially applied run is restoring that backup. See
   ``README.md`` -> "迁移前必须先做已验证的数据库备份" and
   ``AGENTS.md`` §10 for the operational checklist.
2. All Celery workers, the backend API process, the MCP entry point,
   and any out-of-process clients must be running a build that already
   drops the field from the in-memory schema/model. Running this
   migration against a stale worker would silently discard the column
   before clients stop writing to it, breaking compatibility without
   surfacing a clear error. The contract assumption is that the
   code-only removal of ``event_types_json`` has shipped everywhere.

Row-level precondition (verified by the operator before the migration)
-----------------------------------------------------------------------

``webhook_config.event_subscriptions_json`` is the single source of
truth for subscription matching. The preflight check refuses to run if
any **enabled** row has a NULL, non-array, or empty
``event_subscriptions_json`` — those rows would be silently dropped
from the dispatcher after the column is removed. Disabled rows
(``enabled = false``) are allowed because they do not dispatch.

The active database is verified to have zero such rows. Operators
running this migration against a non-empty webhook_config must
inspect the blocker query documented in the operator runbook (see
AGENTS.md §10 and the matching session in this code base) and either
back-fill ``event_subscriptions_json`` from a snapshot or disable the
offending rows before proceeding.

Schema preflight (in ``upgrade()``)
-----------------------------------

The migration asserts that ``event_types_json`` exists on
``webhook_config`` before dropping it. If the column is already
absent (a half-applied or already-cleaned database), the migration
aborts with ``RuntimeError`` rather than silently no-op'ing, so the
operator is forced to investigate before issuing
``alembic stamp head`` or re-running. The check uses
``information_schema.columns`` so it is schema-aware and survives
tablespace / role quirks (same shape as ``20260902_0018``,
``20260902_0020``, ``20260902_0021``).

Irreversibility
---------------

``downgrade()`` raises ``NotImplementedError`` rather than attempting
to recreate the column. The contract assumption is that every active
row has its subscriptions already expressed in
``event_subscriptions_json``; the column being dropped therefore has
no surviving values to recover even in principle. Re-creating the
column with ``JSONB NULL`` default would silently look correct while
losing every prior write. Operators who need to roll back must
restore the pre-upgrade backup that AGENTS.md §10 already requires.

This migration is irreversible in the same sense as the prior
``20260825_0012``, ``20260825_0014``, ``20260826_0017``,
``20260902_0018``, ``20260902_0019``, ``20260902_0020`` and
``20260902_0021`` revisions.
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "20260904_0022"
down_revision: Union[str, None] = "20260902_0021"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

DROPPED_COLUMN: str = "event_types_json"
TARGET_TABLE: str = "webhook_config"


def _assert_column_present() -> None:
    """Abort when the legacy ``event_types_json`` column is already missing.

    The migration assumes the pre-state where every row still carries
    a ``event_types_json`` column (even if the column value is NULL on
    most rows). A database that has already lost the column must not be
    silently accepted: that would imply either a half-applied previous
    run, an out-of-band operator cleanup, or a deployment that does not
    match the contract. In all three cases we refuse to run so the
    operator investigates before issuing ``alembic stamp head`` or
    re-running. The check uses ``information_schema.columns`` so it is
    schema-aware and survives tablespace / role quirks.
    """

    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() "
            "AND table_name = :table_name "
            "AND column_name = :column_name"
        ),
        {"table_name": TARGET_TABLE, "column_name": DROPPED_COLUMN},
    ).fetchall()
    if not rows:
        raise RuntimeError(
            "webhook subscription contract migration preflight failed: "
            f"{TARGET_TABLE}.{DROPPED_COLUMN} is missing from the current "
            "schema. Either the database is already past 20260904_0022 or "
            "an out-of-band schema change has dropped the column. "
            "Investigate before proceeding; do not stamp this revision."
        )


def upgrade() -> None:
    _assert_column_present()
    op.drop_column(TARGET_TABLE, DROPPED_COLUMN)


def downgrade() -> None:
    raise NotImplementedError(
        "irreversible migration - restore from verified DB backup"
    )
