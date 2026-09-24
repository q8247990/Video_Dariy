"""add event_record.focus_matches_json.

Revision ID: 20260923_0024
Revises: 20260907_0023
Create Date: 2026-09-23 12:00:00.000000

Stores the user-defined focus-point keys an event matched. The value is
the vision model's ``focus_matches`` output, already normalized and
filtered to the household's declared focus keys. Nullable JSON:
historical rows keep ``NULL`` and focus statistics treat that as "no
match".

Reversibility
-------------

Adding a nullable column never loses data, so ``upgrade()`` is safe.
``downgrade()`` drops the column; any focus matches written after the
upgrade are lost, so restore from the pre-upgrade backup if that data
must be kept.
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "20260923_0024"
down_revision: Union[str, None] = "20260907_0023"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("event_record", sa.Column("focus_matches_json", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("event_record", "focus_matches_json")
