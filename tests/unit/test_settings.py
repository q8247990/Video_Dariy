"""Unit tests for src.core.config Settings validators (Wave 6.3 / Fix 4)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError


def test_default_settings_load_cleanly() -> None:
    from src.core.config import Settings

    s = Settings()
    assert s.ANALYSIS_LEASE_SECONDS == 300


def test_analysis_chunk_sizing_settings_are_removed() -> None:
    from src.core.config import Settings

    s = Settings()
    assert not hasattr(s, "ANALYZER_SEGMENT_SECONDS")
    assert not hasattr(s, "ANALYZER_LLM_CHUNK_SECONDS")


def test_production_settings_reject_missing_signing_secrets() -> None:
    from src.core.config import Settings

    with pytest.raises(ValidationError, match="Production secret configuration"):
        Settings(APP_ENV="production", SECRET_KEY="", MEDIA_SIGNING_KEY="")


def test_production_settings_require_distinct_signing_secrets() -> None:
    from src.core.config import Settings

    with pytest.raises(ValidationError, match="must be distinct"):
        Settings(
            APP_ENV="production",
            SECRET_KEY="unique-secret",
            MEDIA_SIGNING_KEY="unique-secret",
            PROVIDER_KEY_ENCRYPTION_KEY="v1:HUWgH-KGpzLNiKZ8Fjp51f6zXYQfR6s4vrpSH7U7okY=",
        )
