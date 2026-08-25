"""encrypt legacy LLM provider API keys

Revision ID: 20260825_0012
Revises: 20260825_0011
Create Date: 2026-08-25 13:00:00.000000

This data migration is intentionally irreversible. Back up the database before
upgrading; restore that verified backup if encryption cannot complete.
"""

from typing import Sequence, Union

from sqlalchemy import text

from alembic import op
from src.services.provider_key_crypto import encrypt_provider_api_key, is_encrypted_provider_api_key

revision: str = "20260825_0012"
down_revision: Union[str, None] = "20260825_0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    connection = op.get_bind()
    rows = connection.execute(text("SELECT id, api_key FROM llm_provider")).mappings()
    for row in rows:
        api_key = row["api_key"] or ""
        if api_key and not is_encrypted_provider_api_key(api_key):
            connection.execute(
                text("UPDATE llm_provider SET api_key = :api_key WHERE id = :id"),
                {"api_key": encrypt_provider_api_key(api_key), "id": row["id"]},
            )


def downgrade() -> None:
    raise RuntimeError("Provider-key encryption is irreversible; restore the pre-upgrade backup")
