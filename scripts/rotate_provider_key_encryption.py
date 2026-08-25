"""Re-encrypt LLM provider API keys using the current configured key."""

from __future__ import annotations

import logging
import os

from sqlalchemy import select

from src.db.session import task_db_session
from src.models.llm_provider import LLMProvider
from src.services.provider_key_crypto import (
    decrypt_provider_api_key,
    encrypt_provider_api_key,
    is_encrypted_provider_api_key,
)

logger = logging.getLogger(__name__)


def rotate_provider_key_encryption() -> int:
    """Re-encrypt existing ciphertext atomically with the configured key material."""
    with task_db_session() as db:
        providers = db.scalars(select(LLMProvider).with_for_update()).all()
        rotated_count = 0
        for provider in providers:
            if not provider.api_key:
                continue
            if not is_encrypted_provider_api_key(provider.api_key):
                raise RuntimeError(
                    "Refusing plaintext provider key; restore backup or run migration"
                )
            old_encryption_key = os.environ.get("OLD_PROVIDER_KEY_ENCRYPTION_KEY")
            plaintext_key = decrypt_provider_api_key(provider.api_key, old_encryption_key)
            provider.api_key = encrypt_provider_api_key(plaintext_key)
            rotated_count += 1
        db.commit()
    logger.info("Rotated encryption for %s LLM provider keys", rotated_count)
    return rotated_count


if __name__ == "__main__":
    rotate_provider_key_encryption()
