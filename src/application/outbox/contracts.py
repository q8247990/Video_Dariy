"""Outbox DTO contracts.

This module defines two frozen dataclasses and the round-trip
helpers that the persistence model and the contract tests share:

- :class:`OutboxCommand` is the **in-memory** command the application
  builds. It is what the use case passes to ``emit_event(...)``.
  Fields are typed loosely (``Any`` / ``Iterable``) and the JSON
  validator normalizes them on the way in.
- :class:`OutboxEvent` is the **durable** row. The field list is the
  canonical one from §2 of the ADR; renaming any field is an ADR
  amendment. ``to_row_dict`` produces the kwargs for the SQLAlchemy
  ``INSERT`` (Todo 12); ``from_row_dict`` is used by the contract
  tests to round-trip without standing up a database.

The contract tests live in :mod:`tests.unit.test_outbox_contract` and
assert that ``OutboxEvent`` round-trips, that ``emit_event`` rejects
unknown task names and duplicate event IDs, and that the dataclass
remains ``frozen=True`` under mutation attempts.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from typing import Any, Mapping, Optional
from uuid import UUID

from src.application.outbox.errors import (
    OutboxContractViolation,
    OutboxRegistryError,
)
from src.application.outbox.payload_validator import (
    DEFAULT_MAX_PAYLOAD_BYTES,
    DEFAULT_MAX_PAYLOAD_DEPTH,
    validate_command_payload,
)
from src.application.outbox.registry import DEFAULT_QUEUE, OutboxCommandRegistry
from src.application.outbox.state_machine import OutboxStatus

#: Marker type for JSON-safe payloads. Used in type hints only; the
#: runtime guard is :func:`validate_command_payload`.
JSONSafeValue = Any


#: Process-local set of event IDs that have already been emitted via
#: :func:`emit_event` in this Python process. The DB unique constraint
#: on ``event_id`` is the source of truth; this set is purely a fast
#: early-reject for obvious caller bugs in tests.
_EMITTED_EVENT_IDS: set[str] = set()


def _reset_emitted_event_ids_for_testing() -> None:
    """Clear the in-memory ``_EMITTED_EVENT_IDS`` set.

    Tests call this in their fixtures so a previous test's emitted
    IDs do not leak into the next test.
    """
    _EMITTED_EVENT_IDS.clear()


@dataclass(frozen=True)
class OutboxCommand:
    """In-memory command payload produced by application use cases.

    Attributes:
        task_name: Dotted Celery task name. MUST be registered in
            :class:`OutboxCommandRegistry`; ``emit_event`` enforces
            this.
        queue: Celery queue name. Defaults to the registered queue
            for ``task_name``; callers may override (e.g. analyzer
            picks ``analysis_hot`` vs ``analysis_full``).
        args: Positional arguments for the Celery task. JSON-safe.
        kwargs: Keyword arguments for the Celery task. JSON-safe.
    """

    task_name: str
    queue: str = DEFAULT_QUEUE
    args: tuple[Any, ...] = ()
    kwargs: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.task_name, str) or not self.task_name:
            raise OutboxContractViolation(
                "OutboxCommand.task_name must be a non-empty string",
                payload={"task_name": self.task_name},
            )
        if not isinstance(self.queue, str) or not self.queue:
            raise OutboxContractViolation(
                "OutboxCommand.queue must be a non-empty string",
                payload={"queue": self.queue},
            )
        if not isinstance(self.args, tuple):
            # Frozen dataclasses cannot be coerced in-place; convert
            # lists defensively so callers can pass either.
            object.__setattr__(self, "args", tuple(self.args))
        if not isinstance(self.kwargs, Mapping):
            raise OutboxContractViolation(
                "OutboxCommand.kwargs must be a mapping",
                payload={"kwargs_type": type(self.kwargs).__name__},
            )


@dataclass(frozen=True)
class OutboxEvent:
    """Durable outbox row.

    The field list is canonical and frozen by the ADR. ``to_row_dict``
    produces the kwargs for the SQLAlchemy ``INSERT`` and excludes
    database-managed columns (``id``) so the persistence layer can
    use the dict verbatim as ``OutboxEventRow(**event.to_row_dict())``.

    Attributes:
        id: Surrogate primary key. ``None`` until the row is INSERTed.
        event_id: UUID; reused as the Celery ``task_id`` and the
            consumer-side idempotency key.
        task_log_id: 1:1 link to ``task_log.id``.
        dedupe_key: Optional mirror of ``task_log.dedupe_key``; the
            partial unique index in the DB is on
            ``(task_log_id) WHERE status='pending'``, not on
            ``dedupe_key``.
        task_name: Registered Celery task name.
        queue: Celery queue.
        args_json: JSON-safe list.
        kwargs_json: JSON-safe dict.
        status: Lifecycle state (see :class:`OutboxStatus`).
        attempt_count: Monotonic counter; bumped by the publisher on
            each retryable failure.
        next_attempt_at: Wall-clock time the publisher should next
            try this row. Initial = ``created_at``.
        claimed_by: Publisher instance id while in ``publishing``;
            cleared on transition out.
        lease_expires_at: Publisher lease; cleared when the row
            leaves ``publishing``.
        published_at: Wall-clock success timestamp; ``None`` until
            ``status == PUBLISHED``.
        last_error: Truncated publisher error; ``None`` until first
            failure.
        created_at: Row creation timestamp.
        updated_at: Bumped on every UPDATE.
    """

    event_id: UUID
    task_log_id: int
    task_name: str
    queue: str
    args_json: list[Any]
    kwargs_json: dict[str, Any]
    status: OutboxStatus = OutboxStatus.PENDING
    attempt_count: int = 0
    next_attempt_at: Optional[datetime] = None
    id: Optional[int] = None
    dedupe_key: Optional[str] = None
    claimed_by: Optional[str] = None
    lease_expires_at: Optional[datetime] = None
    published_at: Optional[datetime] = None
    last_error: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    def __post_init__(self) -> None:
        if self.created_at is None:
            object.__setattr__(self, "created_at", datetime.now(tz=timezone.utc))
        if self.updated_at is None:
            object.__setattr__(self, "updated_at", self.created_at)
        if self.next_attempt_at is None:
            object.__setattr__(self, "next_attempt_at", self.created_at)

    # ------------------------------------------------------------------
    # Round-trip helpers
    # ------------------------------------------------------------------

    def to_row_dict(self) -> dict[str, Any]:
        """Return a dict suitable for the SQLAlchemy ``INSERT``.

        Excludes ``id`` so the surrogate key is filled by the
        sequence. Datetimes are left as ``datetime`` objects; the
        persistence layer (Todo 12) is responsible for binding them
        to ``TIMESTAMPTZ`` columns.
        """
        return {
            "event_id": self.event_id,
            "task_log_id": self.task_log_id,
            "dedupe_key": self.dedupe_key,
            "task_name": self.task_name,
            "queue": self.queue,
            "args_json": self.args_json,
            "kwargs_json": self.kwargs_json,
            "status": self.status.value,
            "attempt_count": self.attempt_count,
            "next_attempt_at": self.next_attempt_at,
            "claimed_by": self.claimed_by,
            "lease_expires_at": self.lease_expires_at,
            "published_at": self.published_at,
            "last_error": self.last_error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_row_dict(cls, row: Mapping[str, Any]) -> "OutboxEvent":
        """Construct an :class:`OutboxEvent` from a database row dict.

        Accepts either the full DB row (``id`` populated) or the
        payload returned by :meth:`to_row_dict` (``id`` absent). The
        helper is permissive about ``status`` so the tests can round
        trip both the enum and the raw string.
        """
        kwargs: dict[str, Any] = {}
        for f in fields(cls):
            if f.name not in row:
                continue
            value = row[f.name]
            if f.name == "status" and not isinstance(value, OutboxStatus):
                value = OutboxStatus(value)
            kwargs[f.name] = value
        return cls(**kwargs)


#: Alias for the kwargs shape produced by :meth:`OutboxEvent.to_row_dict`.
#: Used in type hints only; the runtime guard is the dataclass itself.
OutboxRowDict = dict[str, Any]


def new_event_id() -> UUID:
    """Return a fresh event_id UUID.

    Thin wrapper over :func:`uuid.uuid4`. Lives in this module so
    callers do not need to import :mod:`uuid` directly — the test
    suite can monkeypatch a deterministic generator via this name.
    """
    return uuid.uuid4()


def emit_event(
    *,
    command: OutboxCommand,
    task_log_id: int,
    event_id: Optional[UUID] = None,
    dedupe_key: Optional[str] = None,
    now: Optional[datetime] = None,
    max_depth: int = DEFAULT_MAX_PAYLOAD_DEPTH,
    max_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES,
) -> OutboxEvent:
    """Build an :class:`OutboxEvent` from an :class:`OutboxCommand`.

    Validates:

    1. ``command.task_name`` is registered in
       :class:`OutboxCommandRegistry`; raises
       :class:`OutboxRegistryError` otherwise.
    2. ``args`` and ``kwargs`` are JSON-safe; raises
       :class:`OutboxPayloadError` otherwise.
    3. ``event_id`` (auto-generated when ``None``) is unique within
       this Python process; raises :class:`OutboxContractViolation`
       otherwise. The DB unique constraint is the source of truth —
       this is a fast early reject for caller bugs in tests.

    Returns an :class:`OutboxEvent` whose ``status`` is
    :attr:`OutboxStatus.PENDING`. The persistence layer (Todo 12) is
    expected to ``INSERT`` this row in the **same database
    transaction** as the ``task_log`` row.
    """
    if not OutboxCommandRegistry.is_allowed(command.task_name):
        raise OutboxRegistryError(
            f"task_name {command.task_name!r} is not in the outbox registry whitelist",
            payload={"task_name": command.task_name},
        )

    normalized_args, normalized_kwargs = validate_command_payload(
        command.args,
        command.kwargs,
        max_depth=max_depth,
        max_bytes=max_bytes,
    )

    final_event_id = event_id if event_id is not None else new_event_id()
    event_id_str = str(final_event_id)
    if event_id_str in _EMITTED_EVENT_IDS:
        raise OutboxContractViolation(
            "event_id has already been emitted in this process; "
            "duplicate detection at the contract layer (DB constraint "
            "is the source of truth)",
            payload={"event_id": event_id_str},
        )
    _EMITTED_EVENT_IDS.add(event_id_str)

    timestamp = now or datetime.now(tz=timezone.utc)
    return OutboxEvent(
        event_id=final_event_id,
        task_log_id=task_log_id,
        dedupe_key=dedupe_key,
        task_name=command.task_name,
        queue=command.queue,
        args_json=normalized_args,
        kwargs_json=normalized_kwargs,
        status=OutboxStatus.PENDING,
        attempt_count=0,
        next_attempt_at=timestamp,
        created_at=timestamp,
        updated_at=timestamp,
    )


__all__ = [
    "DEFAULT_MAX_PAYLOAD_BYTES",
    "DEFAULT_MAX_PAYLOAD_DEPTH",
    "JSONSafeValue",
    "OutboxCommand",
    "OutboxEvent",
    "OutboxRowDict",
    "emit_event",
    "new_event_id",
]
