"""Unit tests for src.core.config Settings validators (Wave 6.3 / Fix 4)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError


def test_default_settings_load_cleanly() -> None:
    from src.core.config import Settings

    s = Settings()
    assert s.ANALYZER_LLM_CHUNK_SECONDS == 60
    assert s.ANALYZER_SEGMENT_SECONDS == 600


def test_llm_chunk_seconds_must_be_positive() -> None:
    from src.core.config import Settings

    with pytest.raises(ValidationError, match="ANALYZER_LLM_CHUNK_SECONDS must be > 0"):
        Settings(ANALYZER_LLM_CHUNK_SECONDS=0)


def test_llm_chunk_seconds_must_not_exceed_segment_seconds() -> None:
    from src.core.config import Settings

    with pytest.raises(ValidationError, match="must be <= ANALYZER_SEGMENT_SECONDS"):
        Settings(ANALYZER_LLM_CHUNK_SECONDS=700, ANALYZER_SEGMENT_SECONDS=600)


def test_llm_chunk_seconds_equal_to_segment_seconds_is_allowed() -> None:
    from src.core.config import Settings

    s = Settings(ANALYZER_LLM_CHUNK_SECONDS=600, ANALYZER_SEGMENT_SECONDS=600)
    assert s.ANALYZER_LLM_CHUNK_SECONDS == 600


def test_llm_chunk_seconds_smaller_than_segment_seconds_is_allowed() -> None:
    from src.core.config import Settings

    s = Settings(ANALYZER_LLM_CHUNK_SECONDS=120, ANALYZER_SEGMENT_SECONDS=600)
    assert s.ANALYZER_LLM_CHUNK_SECONDS == 120


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
