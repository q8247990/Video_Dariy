from datetime import datetime

import pytest

from src.models.llm_provider import LLMProvider
from src.schemas.llm_provider import LLMProviderResponse
from src.services.provider_key_crypto import (
    ProviderKeyDecryptionError,
    decrypt_provider_api_key,
    encrypt_provider_api_key,
    is_encrypted_provider_api_key,
)


def test_provider_api_key_encrypts_and_decrypts_non_empty_value() -> None:
    encrypted = encrypt_provider_api_key("provider-secret")

    assert encrypted != "provider-secret"
    assert is_encrypted_provider_api_key(encrypted) is True
    assert decrypt_provider_api_key(encrypted) == "provider-secret"


def test_provider_api_key_preserves_keyless_provider_support() -> None:
    assert encrypt_provider_api_key("") == ""
    assert decrypt_provider_api_key("") == ""


def test_provider_api_key_rejects_plaintext_at_consumption_boundary() -> None:
    with pytest.raises(ProviderKeyDecryptionError):
        decrypt_provider_api_key("legacy-plaintext")


def test_provider_model_encrypts_api_key_and_response_masks_it() -> None:
    provider = LLMProvider(
        id=1,
        provider_name="provider",
        provider_type="qa_provider",
        api_base_url="http://localhost:8000/v1",
        api_key="provider-secret",
        model_name="local-model",
        timeout_seconds=60,
        retry_count=3,
        enabled=True,
        supports_vision=False,
        supports_qa=True,
        supports_tool_calling=False,
        is_default_vision=False,
        is_default_qa=False,
        created_at=datetime(2026, 8, 25, 12, 0, 0),
        updated_at=datetime(2026, 8, 25, 12, 0, 0),
    )

    response = LLMProviderResponse.model_validate(provider)

    assert provider.api_key != "provider-secret"
    assert decrypt_provider_api_key(provider.api_key) == "provider-secret"
    assert response.api_key == "configured****"


def test_provider_response_keeps_keyless_provider_empty() -> None:
    provider = LLMProvider(
        id=1,
        provider_name="local-provider",
        provider_type="qa_provider",
        api_base_url="http://localhost:8000/v1",
        api_key="",
        model_name="local-model",
        timeout_seconds=60,
        retry_count=3,
        enabled=True,
        supports_vision=False,
        supports_qa=True,
        supports_tool_calling=False,
        is_default_vision=False,
        is_default_qa=False,
        created_at=datetime(2026, 8, 25, 12, 0, 0),
        updated_at=datetime(2026, 8, 25, 12, 0, 0),
    )

    assert LLMProviderResponse.model_validate(provider).api_key == ""
