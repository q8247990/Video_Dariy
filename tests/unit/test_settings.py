"""Unit tests for src.core.config Settings validators (Wave 6.3 / Fix 4)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError


def test_default_settings_load_cleanly() -> None:
    from src.core.config import Settings

    s = Settings()
    assert s.ANALYZER_LLM_CHUNK_SECONDS == 300
    assert s.ANALYZER_SEGMENT_SECONDS == 600
    assert s.ANALYZER_VIDEO_KEYFRAME_FALLBACK_TO_MP4 is True


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
