"""Structured JSON logging, correlation-id threading and sensitive-field redaction.

Todo 24 (Wave 6) adds operations-grade logging to the service. This
module is the single, stdlib-only home for three concerns:

1. **JSON-structured records** — a :class:`RedactingJsonFormatter`
   emits every log record as a single-line JSON object (timestamp,
   level, logger, correlation id, message). Formatting the message as
   JSON at the *formatter* boundary (rather than calling ``json.dumps``
   at each call site) keeps every existing ``logger.info(...)`` /
   ``logger.exception(...)`` call working unchanged.

2. **Correlation-id threading** — a module-level
   :data:`CORRELATION_CONTEXT` ``ContextVar`` carries the current
   request / task correlation id. The FastAPI request middleware
   (:mod:`src.main`) and the Celery ``task_prerun`` signal
   (:mod:`src.core.celery_app`) set it; the :class:`CorrelationFilter`
   injects it into every emitted record. The outbox ``event_id`` is
   the natural correlation key: a single id links the API dispatch
   log, the publisher's publish log and the worker's execution log.

3. **Sensitive-field redaction** — :func:`redact` scrubs secrets and
   filesystem paths out of log *messages* so operators can share and
   grep logs without leaking material. It is applied by the JSON
   formatter and by a composition that the root handler uses.

Design constraints (ADR / MUST-NOT compliance)
=============================================

- **stdlib only** — no new third-party dependency. Everything is
  ``json`` + ``logging`` + ``contextvars`` + ``re``.
- **pytest / caplog-safe** — :func:`configure_logging` is idempotent
  and installs a handler on the root logger *without mutating existing
  handlers or forcing a level*. ``pytest``'s ``caplog`` attaches its
  own handler and its own message-only formatter, so existing tests
  that assert on ``caplog.text`` (e.g.
  ``test_analyze_session_failure_logs_prompt_and_raw_response``)
  keep passing unchanged: they never consult this module's formatter.
- **does not remove the prompt/LLM-response diagnostics** that the
  analyzer's failure path deliberately logs (a pre-existing,
  test-locked behaviour). :func:`redact` targets *secrets and paths*,
  the genuinely dangerous sensitive data, not arbitrary diagnostic
  text.
"""

from __future__ import annotations

import contextvars
import json
import logging
import re
from typing import Optional

from src.core.config import settings

# ---------------------------------------------------------------------------
# Correlation context
# ---------------------------------------------------------------------------

#: Active correlation id for the current request / Celery task. Set by
#: the FastAPI middleware and the Celery ``task_prerun`` signal; read by
#: :class:`CorrelationFilter`. Uses ``contextvars`` so concurrent async
#: requests and concurrent Celery workers each see their own value.
CORRELATION_CONTEXT: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "correlation_id", default=None
)


def set_correlation_id(value: Optional[str]) -> None:
    """Set the current correlation id for the calling context."""
    CORRELATION_CONTEXT.set(value)


def get_correlation_id() -> Optional[str]:
    """Return the current correlation id (or ``None``)."""
    return CORRELATION_CONTEXT.get()


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------

#: JSON-like ``"key": "value"`` pairs whose *value* should always be
#: treated as sensitive (API keys / tokens / passwords / auth headers)
#: regardless of the surrounding text. The quoted value is replaced by
#: ``"***"`` while the key name is preserved.
_SENSITIVE_KEY_PATTERN = re.compile(
    r'("(?:api[_-]?key|apikey|token|secret|password|passwd|authorization)"'
    r'\s*[:=]\s*")([^"]*)(")',
    re.IGNORECASE,
)


def _candidate_secret_values() -> list[str]:
    """Collect the concrete secret values known to this process."""
    values: list[str] = []
    for name in (
        "SECRET_KEY",
        "MEDIA_SIGNING_KEY",
        "PROVIDER_KEY_ENCRYPTION_KEY",
        "MCP_TOKEN",
        "DEFAULT_ADMIN_PASSWORD",
    ):
        value = getattr(settings, name, None)
        if isinstance(value, str) and value:
            values.append(value)
    # DATABASE_URL / REDIS_URL may embed credentials; extract any
    # ``<user>:<password>@`` password component.
    for name in ("DATABASE_URL", "REDIS_URL"):
        value = getattr(settings, name, None)
        if isinstance(value, str) and "@" in value:
            values.append(value)
    return [v for v in values if len(v) >= 8]


def redact(message: str) -> str:
    """Return ``message`` with known secrets and filesystem paths removed.

    Redacts:

    - the configured secret *values* (:data:`Settings.SECRET_KEY` etc.);
    - entire ``DATABASE_URL`` / ``REDIS_URL`` connection strings;
    - ``Authorization``-style headers and any JSON key whose name is an
      obvious secret (``api_key`` / ``password`` / ``token`` …);
    - absolute paths that begin with :data:`Settings.VIDEO_ROOT_PATH` or
      :data:`Settings.PLAYBACK_CACHE_ROOT` (the on-disk video / cache
      trees operators do not want surfaced).

    The function is a no-op on ordinary text; it never alters the
    message shape of a benign diagnostic.
    """
    if not message:
        return message
    scrubbed = message
    for value in _candidate_secret_values():
        if value and value in scrubbed:
            scrubbed = scrubbed.replace(value, "[REDACTED]")
    for root in (settings.VIDEO_ROOT_PATH, settings.PLAYBACK_CACHE_ROOT):
        if root and root in scrubbed:
            scrubbed = _redact_paths(scrubbed, root)
    scrubbed = _SENSITIVE_KEY_PATTERN.sub(r"\1***\3", scrubbed)
    return scrubbed


def _redact_paths(message: str, root: str) -> str:
    """Mask the portion of ``message`` that lies under ``root``.

    Replaces absolute paths whose head is ``root`` (e.g.
    ``/data/videos/2026/09/03/clip.mp4``) with the root followed by a
    single masked marker (``/data/videos/**``) so the log stays readable
    without exposing the on-disk tree.
    """
    escaped = re.escape(root)
    pattern = re.compile(escaped + r"(?:/[^/\s]+)+")
    return pattern.sub(lambda m: root + "/**", message)


# ---------------------------------------------------------------------------
# Filters / formatters
# ---------------------------------------------------------------------------


class CorrelationFilter(logging.Filter):
    """Inject the active correlation id into every emitted record.

    Adds ``record.correlation_id`` (string or ``None``) so the JSON
    formatter can include it without every call site passing it.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = get_correlation_id()  # type: ignore[attr-defined]
        return True


class RedactingJsonFormatter(logging.Formatter):
    """Render a log record as a single-line JSON object.

    The ``message`` field is the redacted, interpolated, human-readable
    message. Structured extra fields (``*args`` keyword ``extra=`` dict)
    are NOT spread into the JSON — we deliberately keep the output flat
    and predictable for machine parsing and log-scanner friendliness.
    """

    def format(self, record: logging.LogRecord) -> str:
        message = super().format(record)
        message = redact(message)
        payload = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "correlation_id": getattr(record, "correlation_id", None),
            "message": message,
        }
        return json.dumps(payload, ensure_ascii=False, default=str)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_CONFIGURED = False


def configure_logging(level: int = logging.INFO) -> None:
    """Install a single JSON root handler. Idempotent, caplog-safe.

    Attaches one :class:`logging.StreamHandler` to the root logger that
    emits redacted JSON via :class:`RedactingJsonFormatter`, filtered by
    :class:`CorrelationFilter`. Existing handlers are left intact (so
    ``caplog``'s own handler survives), and the root logger level is
    only raised, never lowered below ``level``. Safe to call at import
    time from :mod:`src.main` and :mod:`src.core.celery_app`.
    """
    global _CONFIGURED
    if _CONFIGURED:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(RedactingJsonFormatter())
    handler.addFilter(CorrelationFilter())
    root = logging.getLogger()
    root.addHandler(handler)
    if root.level == logging.NOTSET or root.level > level:
        root.setLevel(level)
    _CONFIGURED = True


__all__ = [
    "CORRELATION_CONTEXT",
    "CorrelationFilter",
    "RedactingJsonFormatter",
    "configure_logging",
    "get_correlation_id",
    "redact",
    "set_correlation_id",
]
