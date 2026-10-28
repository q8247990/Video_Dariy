"""JSON-safety guard for broker payloads.

The broker only transports JSON, so a dispatch intent carrying bytes, sets
or arbitrary objects must fail at the dispatch site rather than inside the
worker. :func:`ensure_json_safe` rejects those shapes and returns the value
unchanged otherwise.
"""

from __future__ import annotations

from typing import Any

MAX_PAYLOAD_DEPTH = 8
MAX_PAYLOAD_BYTES = 32 * 1024


class TaskPayloadError(ValueError):
    """Raised when a dispatch payload cannot be serialised to JSON."""


def _check(value: Any, depth: int) -> None:
    if depth > MAX_PAYLOAD_DEPTH:
        raise TaskPayloadError(f"payload nesting deeper than {MAX_PAYLOAD_DEPTH}")
    if isinstance(value, (bytes, bytearray, memoryview, set, frozenset)):
        raise TaskPayloadError(f"{type(value).__name__} is not JSON-safe")
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise TaskPayloadError("payload dict keys must be strings")
            _check(item, depth + 1)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _check(item, depth + 1)


def ensure_json_safe(value: Any) -> Any:
    _check(value, 0)
    return value


__all__ = ["MAX_PAYLOAD_BYTES", "MAX_PAYLOAD_DEPTH", "TaskPayloadError", "ensure_json_safe"]
