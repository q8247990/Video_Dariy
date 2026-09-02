"""Payload validator contract tests (ADR 0011 §5).

These tests lock the JSON-safety rules of outbox payloads:

- atomic types (``None``, ``bool``, ``int``, ``float``, ``str``,
  ``UUID``, ``datetime``, ``date``) are accepted;
- container types (``list``, ``tuple``, ``dict`` with string keys) are
  accepted, with tuples collapsed to lists;
- ``bytes`` / ``bytearray`` / ``memoryview`` / arbitrary objects /
  ``set`` / ``frozenset`` / dict with non-string keys are rejected
  with :class:`OutboxPayloadError` and a JSON-pointer-style path;
- oversized payloads (depth or bytes) are rejected;
- the non-raising :func:`is_json_safe` helper agrees with the raising
  :func:`ensure_json_safe`.

The tests are hermetic: no database, no Celery, no monkeypatching.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from uuid import UUID

import pytest

from src.application.outbox.errors import OutboxPayloadError
from src.application.outbox.payload_validator import (
    DEFAULT_MAX_PAYLOAD_BYTES,
    DEFAULT_MAX_PAYLOAD_DEPTH,
    ensure_json_safe,
    is_json_safe,
    validate_command_payload,
)

# ---------------------------------------------------------------------------
# Accept paths
# ---------------------------------------------------------------------------


def test_accepts_none() -> None:
    assert ensure_json_safe(None) is None
    assert is_json_safe(None)


def test_accepts_primitives() -> None:
    assert ensure_json_safe(True) is True
    assert ensure_json_safe(42) == 42
    assert ensure_json_safe(3.14) == 3.14
    assert ensure_json_safe("hello") == "hello"
    assert is_json_safe(True)
    assert is_json_safe(42)
    assert is_json_safe(3.14)
    assert is_json_safe("hello")


def test_accepts_uuid() -> None:
    value = UUID("00000000-0000-0000-0000-000000000001")
    assert ensure_json_safe(value) is value
    assert is_json_safe(value)


def test_accepts_aware_datetime() -> None:
    value = datetime(2026, 9, 2, 12, 0, 0, tzinfo=timezone.utc)
    assert ensure_json_safe(value) is value
    assert is_json_safe(value)


def test_accepts_date() -> None:
    value = date(2026, 9, 2)
    assert ensure_json_safe(value) is value
    assert is_json_safe(value)


def test_accepts_list_and_tuple_collapses_to_list() -> None:
    value = (1, 2, [3, 4])
    assert ensure_json_safe(value) == [1, 2, [3, 4]]
    assert is_json_safe(value)


def test_accepts_dict_with_string_keys() -> None:
    value = {"session_id": 42, "priority": "hot", "nested": {"k": "v"}}
    assert ensure_json_safe(value) == value
    assert is_json_safe(value)


def test_accepts_deeply_nested_payload_at_max_depth() -> None:
    # Build a payload whose deepest leaf is exactly max_depth containers
    # below the root.
    deepest = 1
    payload: object = deepest
    for _ in range(DEFAULT_MAX_PAYLOAD_DEPTH):
        payload = [payload]
    # The top-level list bumps the depth by one on entry; max_depth
    # counts against the first container entry. ensure_json_safe must
    # accept a payload whose total nesting is exactly max_depth + 1
    # containers (one for the root call, plus max_depth nested).
    # See :func:`ensure_json_safe` for the precise counting.
    normalized = ensure_json_safe(payload, current_depth=0)
    assert normalized == payload


# ---------------------------------------------------------------------------
# Reject paths
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        b"\x00\x01",
        bytearray(b"\x00\x01"),
        memoryview(b"\x00\x01"),
    ],
    ids=["bytes", "bytearray", "memoryview"],
)
def test_rejects_binary_types(value: object) -> None:
    with pytest.raises(OutboxPayloadError, match="non-JSON-safe type"):
        ensure_json_safe(value)
    assert is_json_safe(value) is False


def test_rejects_set_and_frozenset() -> None:
    with pytest.raises(OutboxPayloadError, match="non-JSON-safe type"):
        ensure_json_safe({1, 2, 3})
    with pytest.raises(OutboxPayloadError, match="non-JSON-safe type"):
        ensure_json_safe(frozenset([1, 2, 3]))
    assert is_json_safe({1, 2, 3}) is False


def test_rejects_arbitrary_object() -> None:
    class Custom:
        pass

    with pytest.raises(OutboxPayloadError, match="non-JSON-safe type"):
        ensure_json_safe(Custom())
    assert is_json_safe(Custom()) is False


def test_rejects_dict_with_non_string_keys() -> None:
    with pytest.raises(OutboxPayloadError, match="non-string dict key"):
        ensure_json_safe({1: "x"})
    assert is_json_safe({1: "x"}) is False


def test_rejects_payload_exceeding_max_depth() -> None:
    deepest = 1
    payload: object = deepest
    for _ in range(DEFAULT_MAX_PAYLOAD_DEPTH + 2):
        payload = [payload]
    with pytest.raises(OutboxPayloadError, match="max depth"):
        ensure_json_safe(payload, current_depth=0)
    assert is_json_safe(payload) is False


def test_payload_path_includes_offending_index() -> None:
    payload = [{"ok": 1}, {"bad": b"\x00"}]
    with pytest.raises(OutboxPayloadError) as excinfo:
        ensure_json_safe(payload, current_depth=0)
    message = str(excinfo.value)
    # Path must pinpoint $.bad or $[1].bad
    assert "$" in message
    assert "bad" in message or "[1]" in message


def test_validate_command_payload_rejects_oversized_bytes() -> None:
    huge = "x" * (DEFAULT_MAX_PAYLOAD_BYTES + 1)
    with pytest.raises(OutboxPayloadError, match="max size"):
        validate_command_payload((), {"big": huge})


def test_validate_command_payload_accepts_normal_command() -> None:
    args, kwargs = validate_command_payload(
        (42, "session_id"),
        {"priority": "hot"},
    )
    assert args == [42, "session_id"]
    assert kwargs == {"priority": "hot"}


def test_validate_command_payload_collapses_tuples() -> None:
    args, kwargs = validate_command_payload((1, 2, (3, 4)), {"k": (5, 6)})
    assert args == [1, 2, [3, 4]]
    assert kwargs == {"k": [5, 6]}


def test_validate_command_payload_rejects_bytes_in_args() -> None:
    with pytest.raises(OutboxPayloadError, match=r"\$\.args\[0\]"):
        validate_command_payload((b"\x00",), {})


def test_validate_command_payload_rejects_bytes_in_kwargs() -> None:
    with pytest.raises(OutboxPayloadError, match=r"\$\.kwargs\.session_id"):
        validate_command_payload((), {"session_id": b"\x00"})
