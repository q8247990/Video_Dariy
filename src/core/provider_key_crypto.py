"""Encrypted-at-rest handling for LLM provider API keys."""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken

from src.core.config import settings

_CIPHERTEXT_PREFIX = "pk:v1:"
_KEY_VERSION = "v1"


class ProviderKeyCryptoError(ValueError):
    """Base class for provider-key encryption configuration and data failures."""


class ProviderKeyConfigurationError(ProviderKeyCryptoError):
    """The configured versioned encryption key is malformed."""


class ProviderKeyDecryptionError(ProviderKeyCryptoError):
    """A stored provider key is not valid ciphertext for the configured key."""


def _fernet_from_versioned_key(versioned_key: str) -> Fernet:
    version, separator, encoded_key = versioned_key.partition(":")
    if separator != ":" or version != _KEY_VERSION or not encoded_key:
        raise ProviderKeyConfigurationError("Invalid provider key encryption configuration")
    try:
        return Fernet(encoded_key.encode("ascii"))
    except (TypeError, ValueError) as exc:
        raise ProviderKeyConfigurationError(
            "Invalid provider key encryption configuration"
        ) from exc


def encrypt_provider_api_key(api_key: str, encryption_key: str | None = None) -> str:
    """Encrypt a non-empty provider key, preserving keyless provider support."""
    if not api_key:
        return ""
    fernet = _fernet_from_versioned_key(encryption_key or settings.PROVIDER_KEY_ENCRYPTION_KEY)
    token = fernet.encrypt(api_key.encode("utf-8")).decode("ascii")
    return f"{_CIPHERTEXT_PREFIX}{token}"


def decrypt_provider_api_key(stored_value: str, encryption_key: str | None = None) -> str:
    """Decrypt stored ciphertext only where an outbound provider client needs it."""
    if not stored_value:
        return ""
    if not stored_value.startswith(_CIPHERTEXT_PREFIX):
        raise ProviderKeyDecryptionError("Provider API key is not encrypted")
    token = stored_value.removeprefix(_CIPHERTEXT_PREFIX)
    try:
        fernet = _fernet_from_versioned_key(encryption_key or settings.PROVIDER_KEY_ENCRYPTION_KEY)
        return fernet.decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeDecodeError, ValueError) as exc:
        raise ProviderKeyDecryptionError("Provider API key cannot be decrypted") from exc


def is_encrypted_provider_api_key(stored_value: str) -> bool:
    """Return whether a non-empty stored value has this release's ciphertext envelope."""
    return bool(stored_value) and stored_value.startswith(_CIPHERTEXT_PREFIX)


def mask_provider_api_key(stored_value: str) -> str:
    """Produce a safe display value without exposing plaintext or ciphertext."""
    if not stored_value:
        return ""
    return "configured****"
