"""Unit tests for structured JSON logging, correlation ids and redaction.

These tests pin the behaviour of :mod:`src.core.logging_config` without
touching the live process logger (so they never interfere with other
tests or ``caplog``). They cover:

- :func:`redact` scrubbing known secret values and video/cache paths.
- :class:`RedactingJsonFormatter` emitting a single-line JSON record
  whose ``message`` is redacted and whose ``correlation_id`` reflects
  the active :data:`CORRELATION_CONTEXT`.
- :class:`CorrelationFilter` injecting the active correlation id.
- :func:`configure_logging` being idempotent.
- JSON-formatted logs containing known secrets / paths show no hits.
"""

from __future__ import annotations

import json
import logging

import pytest

from src.core import logging_config
from src.core.config import settings
from src.core.logging_config import (
    CorrelationFilter,
    RedactingJsonFormatter,
    configure_logging,
    redact,
    set_correlation_id,
)

# ---------------------------------------------------------------------------
# redact()
# ---------------------------------------------------------------------------


def test_redact_scrubs_secret_values() -> None:
    secret = settings.SECRET_KEY
    result = redact(f"request failed with key={secret} and extra noise")
    assert secret not in result
    assert "[REDACTED]" in result


def test_redact_scrubs_database_url() -> None:
    url = settings.DATABASE_URL
    assert "@" in url  # sanity: the configured URL embeds credentials
    result = redact(f"connecting to {url}")
    assert url not in result


def test_redact_scrubs_video_root_path() -> None:
    root = settings.VIDEO_ROOT_PATH
    path = f"{root}/2026/09/03/clip.mp4"
    result = redact(f"processing {path} now")
    assert path not in result
    assert f"{root}/**" in result


def test_redact_scrubs_sensitive_json_key() -> None:
    msg = '{"model": "x", "api_key": "sk-abcdef123456", "prompt": "hi"}'
    result = redact(msg)
    assert "sk-abcdef123456" not in result
    assert "prompt" in result  # benign keys are left intact


def test_redact_is_noop_on_benign_text() -> None:
    msg = "heartbeat completed normally: dispatched_hot=0"
    assert redact(msg) == msg


# ---------------------------------------------------------------------------
# Formatter / filter
# ---------------------------------------------------------------------------


def _emit_through(record_message: str) -> dict:
    formatter = RedactingJsonFormatter()
    filter_ = CorrelationFilter()
    record = logging.LogRecord(
        name="test.logger",
        level=logging.INFO,
        pathname=__file__,
        lineno=10,
        msg=record_message,
        args=(),
        exc_info=None,
    )
    assert filter_.filter(record)
    payload = json.loads(formatter.format(record))
    return payload


def test_formatter_emits_single_line_json_with_correlation() -> None:
    set_correlation_id("corr-123")
    try:
        payload = _emit_through("analysis finished session=42")
    finally:
        set_correlation_id(None)
    assert payload["message"] == "analysis finished session=42"
    assert payload["correlation_id"] == "corr-123"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "test.logger"
    assert "timestamp" in payload


def test_formatter_redacts_secret_in_message() -> None:
    secret = settings.SECRET_KEY
    payload = _emit_through(f"failed: token={secret}")
    assert secret not in payload["message"]
    assert "[REDACTED]" in payload["message"]


def test_correlation_filter_defaults_to_none_when_unset() -> None:
    set_correlation_id(None)
    payload = _emit_through("no correlation")
    assert payload["correlation_id"] is None


# ---------------------------------------------------------------------------
# configure_logging()
# ---------------------------------------------------------------------------


def test_configure_logging_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    set_correlation_id(None)
    monkeypatch.setattr(logging_config, "_CONFIGURED", False)
    configure_logging()
    root = logging.getLogger()
    pre_handlers = list(root.handlers)
    configure_logging()  # second call must be a no-op
    assert root.handlers == pre_handlers


# ---------------------------------------------------------------------------
# Sensitive-field acceptance: formatted logs show no known secrets/paths.
# ---------------------------------------------------------------------------


def test_structured_logs_show_no_known_secrets_or_paths() -> None:
    secret = settings.SECRET_KEY
    media = settings.MEDIA_SIGNING_KEY
    provider_key = settings.PROVIDER_KEY_ENCRYPTION_KEY
    video_path = f"{settings.VIDEO_ROOT_PATH}/nested/cam/rec.mp4"
    sample = (
        "request handler: db_url=%(db_url)s key=%(secret)s media=%(media)s "
        "provider=%(provider)s path=%(path)s"
    ) % {
        "db_url": settings.DATABASE_URL,
        "secret": secret,
        "media": media,
        "provider": provider_key,
        "path": video_path,
    }
    payload = _emit_through(sample)
    emitted = payload["message"]
    for marker in (secret, media, provider_key, settings.DATABASE_URL, video_path):
        assert marker not in emitted, f"leaked sensitive value: {marker!r}"
