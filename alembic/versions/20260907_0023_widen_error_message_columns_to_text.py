"""widen error-message columns from VARCHAR(512) to TEXT.

Revision ID: 20260907_0023
Revises: 20260904_0022
Create Date: 2026-09-07 12:00:00.000000

Why this migration exists
-------------------------

``task_log.message`` (and the three sibling error-message columns) are
written with ``str(exc)`` on the failure path. A PostgreSQL error's
``str()`` embeds the full SQL statement plus bound parameters, which
easily exceeds 512 characters. When the write overflowed
``VARCHAR(512)`` the finalize INSERT itself raised
``StringDataRightTruncation``, the TaskLog row stayed in
``running``, and the heartbeat guard treated it as an active scan —
deadlocking the entire pipeline (2026-09-05 incident).

``Text`` is the right type for unbounded diagnostic text; a larger
fixed ``VARCHAR(N)`` would just be breached again by a bigger payload.
The columns affected:

- ``task_log.message``
- ``llm_provider.last_test_message``
- ``video_source.last_validate_message``
- ``video_file.parse_message``

``tag_definition.description`` is user input, not error text, and is
deliberately left as ``VARCHAR(512)``.

Reversibility
-------------

Widening never loses data, so ``upgrade()`` is unconditionally safe.
``downgrade()`` narrows back to ``VARCHAR(512)``; PostgreSQL refuses
the cast when any stored value would be truncated, which is the
desired guard (roll back only when the values actually fit, otherwise
restore from the pre-upgrade backup).
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "20260907_0023"
down_revision: Union[str, None] = "20260904_0022"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# (table, column) pairs widened to TEXT.
WIDENED_COLUMNS: tuple[tuple[str, str], ...] = (
    ("task_log", "message"),
    ("llm_provider", "last_test_message"),
    ("video_source", "last_validate_message"),
    ("video_file", "parse_message"),
)


def upgrade() -> None:
    for table, column in WIDENED_COLUMNS:
        op.alter_column(
            table, column, existing_type=sa.String(512), type_=sa.Text(), existing_nullable=True
        )


def downgrade() -> None:
    for table, column in reversed(WIDENED_COLUMNS):
        # Narrows TEXT -> VARCHAR(512); PostgreSQL raises an error if
        # any stored value would be truncated (data-loss guard).
        op.alter_column(
            table, column, existing_type=sa.Text(), type_=sa.String(512), existing_nullable=True
        )
