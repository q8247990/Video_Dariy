"""JSON-safety validation for outbox payloads.

The outbox is **not** a content store. ``args_json`` and
``kwargs_json`` must round-trip through :func:`json.dumps` and
:func:`json.loads` while preserving the original Python types within
the allow-list below.

The rules (ADR §5):

- **Allowed atomic types**: ``None``, ``bool``, ``int``, ``float``,
  ``str``, ``UUID`` (serialized as canonical hex), ``datetime`` /
  ``date`` (ISO 8601 UTC, timezone-aware).
- **Allowed container types**: ``list``, ``tuple`` (serialized as
  ``list``), ``dict`` (string keys only).
- **Rejected**: ``bytes`` / ``bytearray`` / ``memoryview``, arbitrary
  Python objects (``object`` not in the allow-list), ``set`` /
  ``frozenset`` (unstable iteration order), dicts with non-string keys,
  structures deeper than :data:`DEFAULT_MAX_PAYLOAD_DEPTH`, payloads
  larger than :data:`DEFAULT_MAX_PAYLOAD_BYTES` after serialization.

The validator is **fail-fast**: it raises :class:`OutboxPayloadError`
on the first offending value, including the JSON-pointer-style path
(``$``, ``$.args[2]``, ``$.kwargs.session_id``) so operators can find
the offending caller without a debugger.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any, Iterable, Mapping
from uuid import UUID

from src.application.outbox.errors import OutboxPayloadError

#: Atomic Python types the outbox accepts as payload leaves.
#: ``tuple`` lives in :data:`JSON_SAFE_CONTAINER_TYPES` because it
#: collapses to ``list`` after ``json.dumps``.
JSON_SAFE_ATOMIC_TYPES: tuple[type, ...] = (
    type(None),
    bool,
    int,
    float,
    str,
    UUID,
    datetime,
    date,
)

#: Container types the outbox accepts. ``dict`` keys MUST be strings
#: (enforced by :func:`_check_dict_keys`).
JSON_SAFE_CONTAINER_TYPES: tuple[type, ...] = (list, tuple, dict)

#: Maximum nesting depth. ``args=[1,[2,[3,[4,…]]]]`` deeper than this
#: is rejected; a typical analyze-session command is depth ≤ 4.
DEFAULT_MAX_PAYLOAD_DEPTH: int = 8

#: Maximum serialized payload size. The serializer uses UTF-8 to
#: count bytes; 32 KiB is a hard ceiling that fits comfortably in a
#: JSONB column without TOAST overhead.
DEFAULT_MAX_PAYLOAD_BYTES: int = 32 * 1024


def _path(parent: str, segment: str) -> str:
    """Append a segment to a JSON-pointer-style path."""
    return f"{parent}{segment}"


def _check_dict_keys(value: Mapping[Any, Any], path: str) -> None:
    """Reject dicts whose keys are not strings."""
    for key in value.keys():
        if not isinstance(key, str):
            raise OutboxPayloadError(
                f"outbox payload at {path} has non-string dict key "
                f"({type(key).__name__}); only str keys are JSON-safe",
                payload={"path": path, "key_type": type(key).__name__},
            )


def _check_depth(
    value: Any,
    *,
    path: str,
    current_depth: int,
    max_depth: int,
) -> None:
    if current_depth > max_depth:
        raise OutboxPayloadError(
            f"outbox payload exceeds max depth {max_depth} at {path}",
            payload={"path": path, "max_depth": max_depth},
        )
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _check_depth(
                item,
                path=_path(path, f"[{index}]"),
                current_depth=current_depth + 1,
                max_depth=max_depth,
            )
    elif isinstance(value, dict):
        for key, item in value.items():
            _check_depth(
                item,
                path=_path(path, f".{key}"),
                current_depth=current_depth + 1,
                max_depth=max_depth,
            )


def is_json_safe(
    value: Any,
    *,
    max_depth: int = DEFAULT_MAX_PAYLOAD_DEPTH,
    max_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES,
) -> bool:
    """Return True if ``value`` is acceptable as an outbox payload.

    Non-raising variant of :func:`ensure_json_safe`. The size limit is
    only checked when the value passes the type rules; oversized
    values return ``False`` rather than raising so callers can branch
    on the result.
    """
    try:
        encoded = ensure_json_safe(
            value,
            path="$",
            current_depth=0,
            max_depth=max_depth,
        )
    except OutboxPayloadError:
        return False
    try:
        serialized = json.dumps(encoded, default=_json_default)
    except (TypeError, ValueError):
        return False
    return len(serialized.encode("utf-8")) <= max_bytes


def ensure_json_safe(
    value: Any,
    *,
    path: str = "$",
    current_depth: int = 0,
    max_depth: int = DEFAULT_MAX_PAYLOAD_DEPTH,
) -> Any:
    """Validate ``value`` recursively and return a JSON-safe copy.

    Tuples are collapsed to lists; ``datetime`` / ``date`` / ``UUID``
    are converted to strings. Other types pass through by reference
    (the dataclass command is built fresh by the caller).
    """
    if isinstance(value, (list, tuple)):
        if current_depth > max_depth:
            raise OutboxPayloadError(
                f"outbox payload exceeds max depth {max_depth} at {path}",
                payload={"path": path, "max_depth": max_depth},
            )
        converted: list[Any] = []
        for index, item in enumerate(value):
            converted.append(
                ensure_json_safe(
                    item,
                    path=_path(path, f"[{index}]"),
                    current_depth=current_depth + 1,
                    max_depth=max_depth,
                )
            )
        return converted
    if isinstance(value, dict):
        if current_depth > max_depth:
            raise OutboxPayloadError(
                f"outbox payload exceeds max depth {max_depth} at {path}",
                payload={"path": path, "max_depth": max_depth},
            )
        _check_dict_keys(value, path)
        converted_dict: dict[str, Any] = {}
        for key, item in value.items():
            converted_dict[key] = ensure_json_safe(
                item,
                path=_path(path, f".{key}"),
                current_depth=current_depth + 1,
                max_depth=max_depth,
            )
        return converted_dict
    if isinstance(value, JSON_SAFE_ATOMIC_TYPES):
        return value
    # Anything else (bytes, set, arbitrary objects, etc.) is rejected.
    raise OutboxPayloadError(
        f"outbox payload at {path} has non-JSON-safe type "
        f"{type(value).__name__}; allowed atomic types are "
        "None/bool/int/float/str/UUID/datetime/date",
        payload={"path": path, "type": type(value).__name__},
    )


def _json_default(value: Any) -> Any:
    """``json.dumps`` fallback for atomic outbox types.

    Called by :func:`validate_command_payload` for the size check.
    Mirrors the conversions in :func:`ensure_json_safe` so the size
    estimate matches what PostgreSQL ``jsonb`` will actually store.
    """
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        # Always emit timezone-aware ISO 8601 UTC. Naive datetimes are
        # treated as UTC (the project storage convention; see
        # AGENTS.md §10 / migration 20260825_0014).
        if value.tzinfo is None:
            return value.replace(tzinfo=None).isoformat() + "+00:00"
        return value.astimezone().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    raise TypeError(f"object of type {type(value).__name__} is not JSON-safe")


def validate_command_payload(
    args: Iterable[Any] | None,
    kwargs: Mapping[str, Any] | None,
    *,
    max_depth: int = DEFAULT_MAX_PAYLOAD_DEPTH,
    max_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES,
) -> tuple[list[Any], dict[str, Any]]:
    """Validate and JSON-normalize an outbox command payload.

    Returns a ``(args_list, kwargs_dict)`` tuple where the args have
    been converted to ``list`` (tuples are collapsed) and every
    value has been JSON-normalized. The size check is applied to the
    serialized form; an oversize payload raises
    :class:`OutboxPayloadError`.
    """
    args_list = list(args or [])
    kwargs_dict = dict(kwargs or {})

    normalized_args = ensure_json_safe(
        args_list,
        path="$.args",
        current_depth=1,
        max_depth=max_depth,
    )
    normalized_kwargs = ensure_json_safe(
        kwargs_dict,
        path="$.kwargs",
        current_depth=1,
        max_depth=max_depth,
    )

    serialized_args = json.dumps(normalized_args, default=_json_default)
    serialized_kwargs = json.dumps(normalized_kwargs, default=_json_default)
    total_bytes = len(serialized_args.encode("utf-8")) + len(serialized_kwargs.encode("utf-8"))
    if total_bytes > max_bytes:
        raise OutboxPayloadError(
            f"outbox payload exceeds max size {max_bytes} bytes (actual {total_bytes})",
            payload={"max_bytes": max_bytes, "actual_bytes": total_bytes},
        )

    return normalized_args, normalized_kwargs


__all__ = [
    "DEFAULT_MAX_PAYLOAD_BYTES",
    "DEFAULT_MAX_PAYLOAD_DEPTH",
    "JSON_SAFE_ATOMIC_TYPES",
    "JSON_SAFE_CONTAINER_TYPES",
    "ensure_json_safe",
    "is_json_safe",
    "validate_command_payload",
]
