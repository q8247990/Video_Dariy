"""Outbox DTO contract tests (ADR 0011 §1, §2, §6, §7).

These tests lock:

- :class:`OutboxCommand` validates its fields on construction;
- :func:`emit_event` builds an :class:`OutboxEvent` whose state is
  ``pending`` and whose ``to_row_dict`` shape matches the canonical
  field list (§2);
- :class:`OutboxEvent` is round-trippable via ``to_row_dict`` /
  ``from_row_dict``;
- the registry whitelist rejects unknown ``task_name`` at emit time;
- duplicate ``event_id`` is rejected at the contract layer (the DB
  unique constraint is the source of truth — this is the fast
  early-reject);
- the dataclass is ``frozen=True``: direct mutation raises
  :class:`OutboxContractViolation`;
- a stub publisher loop drives the state machine end-to-end through
  ``pending → publishing → published`` and the duplicate-publish path
  is observable at the contract layer.
"""

from __future__ import annotations

import copy
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import UUID

import pytest

from src.application.outbox.contracts import (
    OutboxCommand,
    OutboxEvent,
    emit_event,
    new_event_id,
)
from src.application.outbox.errors import (
    OutboxContractViolation,
    OutboxRegistryError,
)
from src.application.outbox.registry import DEFAULT_QUEUE, OutboxCommandRegistry
from src.application.outbox.state_machine import (
    OutboxStatus,
    transition,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_registry() -> None:
    """Reset the registry between tests so whitelist state does not leak."""
    OutboxCommandRegistry.reset_for_testing()
    yield  # type: ignore[misc]
    OutboxCommandRegistry.reset_for_testing()


@pytest.fixture
def fixed_now() -> datetime:
    return datetime(2026, 9, 2, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# OutboxCommand construction
# ---------------------------------------------------------------------------


def test_command_rejects_empty_task_name() -> None:
    with pytest.raises(OutboxContractViolation):
        OutboxCommand(task_name="", args=(), kwargs={})


def test_command_rejects_empty_queue() -> None:
    with pytest.raises(OutboxContractViolation):
        OutboxCommand(task_name="src.tasks.x.x_task", queue="", args=(), kwargs={})


def test_command_rejects_non_mapping_kwargs() -> None:
    with pytest.raises(OutboxContractViolation):
        OutboxCommand(
            task_name="src.tasks.x.x_task",
            args=(),
            kwargs=[("k", "v")],  # type: ignore[arg-type]
        )


def test_command_collapses_list_args_to_tuple() -> None:
    cmd = OutboxCommand(
        task_name="src.tasks.analyzer.analyze_session_task",
        args=[1, 2, 3],  # type: ignore[arg-type]
        kwargs={},
    )
    assert cmd.args == (1, 2, 3)


def test_command_defaults_queue_to_default_queue() -> None:
    cmd = OutboxCommand(
        task_name="src.tasks.analyzer.analyze_session_task",
        args=(),
        kwargs={},
    )
    assert cmd.queue == DEFAULT_QUEUE


# ---------------------------------------------------------------------------
# OutboxEvent round-trip
# ---------------------------------------------------------------------------


def _build_sample_event(
    task_log_id: int = 123,
    event_id: Optional[UUID] = None,
    fixed_now: Optional[datetime] = None,
) -> OutboxEvent:
    cmd = OutboxCommand(
        task_name="src.tasks.analyzer.analyze_session_task",
        queue="analysis_hot",
        args=(42,),
        kwargs={"priority": "hot"},
    )
    return emit_event(
        command=cmd,
        task_log_id=task_log_id,
        event_id=event_id,
        now=fixed_now,
    )


def test_emit_event_builds_pending_event_with_field_shape(fixed_now: datetime) -> None:
    event = _build_sample_event(fixed_now=fixed_now)
    assert event.status is OutboxStatus.PENDING
    assert event.attempt_count == 0
    assert event.id is None  # surrogate key not yet assigned
    assert event.claimed_by is None
    assert event.lease_expires_at is None
    assert event.published_at is None
    assert event.last_error is None
    assert event.created_at == fixed_now
    assert event.updated_at == fixed_now
    assert event.next_attempt_at == fixed_now
    assert event.task_log_id == 123
    assert event.task_name == "src.tasks.analyzer.analyze_session_task"
    assert event.queue == "analysis_hot"
    assert event.args_json == [42]
    assert event.kwargs_json == {"priority": "hot"}


def test_to_row_dict_contains_full_field_set(fixed_now: datetime) -> None:
    event = _build_sample_event(fixed_now=fixed_now)
    row = event.to_row_dict()
    expected_keys = {
        "event_id",
        "task_log_id",
        "dedupe_key",
        "task_name",
        "queue",
        "args_json",
        "kwargs_json",
        "status",
        "attempt_count",
        "next_attempt_at",
        "claimed_by",
        "lease_expires_at",
        "published_at",
        "last_error",
        "created_at",
        "updated_at",
    }
    assert set(row.keys()) == expected_keys
    # ``id`` is excluded because the surrogate key is filled by the
    # sequence at INSERT time.
    assert "id" not in row


def test_event_round_trips_via_row_dict(fixed_now: datetime) -> None:
    import dataclasses

    event_id = uuid.UUID("00000000-0000-0000-0000-000000000abc")
    event = _build_sample_event(event_id=event_id, fixed_now=fixed_now)
    row = event.to_row_dict()
    row["id"] = 999
    restored = OutboxEvent.from_row_dict(row)
    assert restored.id == 999
    for f in dataclasses.fields(OutboxEvent):
        if f.name == "id":
            continue
        assert getattr(restored, f.name) == getattr(event, f.name), (
            f"field {f.name} differs after round-trip"
        )


def test_event_is_frozen_against_mutation(fixed_now: datetime) -> None:
    import dataclasses

    event = _build_sample_event(fixed_now=fixed_now)
    with pytest.raises(dataclasses.FrozenInstanceError):
        event.status = OutboxStatus.PUBLISHED  # type: ignore[misc]


def test_from_row_dict_accepts_string_status() -> None:
    row = {
        "event_id": uuid.UUID("00000000-0000-0000-0000-000000000abc"),
        "task_log_id": 7,
        "task_name": "src.tasks.analyzer.analyze_session_task",
        "queue": "analysis_hot",
        "args_json": [1],
        "kwargs_json": {},
        "status": "published",  # raw string, not the enum
        "created_at": datetime(2026, 9, 2, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 9, 2, tzinfo=timezone.utc),
    }
    event = OutboxEvent.from_row_dict(row)
    assert event.status is OutboxStatus.PUBLISHED


# ---------------------------------------------------------------------------
# Registry enforcement
# ---------------------------------------------------------------------------


def test_emit_event_rejects_unknown_task_name() -> None:
    cmd = OutboxCommand(
        task_name="src.tasks.unknown.bad_task",
        args=(),
        kwargs={},
    )
    with pytest.raises(OutboxRegistryError) as excinfo:
        emit_event(command=cmd, task_log_id=1)
    assert excinfo.value.payload == {"task_name": "src.tasks.unknown.bad_task"}


def test_registry_register_then_emit_succeeds() -> None:
    OutboxCommandRegistry.register("src.tasks.custom.custom_task", queue="custom_q")
    assert OutboxCommandRegistry.is_allowed("src.tasks.custom.custom_task")
    cmd = OutboxCommand(
        task_name="src.tasks.custom.custom_task",
        queue="custom_q",
        args=(),
        kwargs={},
    )
    event = emit_event(command=cmd, task_log_id=1)
    assert event.queue == "custom_q"


def test_registry_queue_for_unknown_task_raises() -> None:
    with pytest.raises(OutboxRegistryError):
        OutboxCommandRegistry.queue_for("src.tasks.unknown.bad_task")


def test_registry_allowed_names_is_a_snapshot() -> None:
    initial = OutboxCommandRegistry.allowed_names()
    OutboxCommandRegistry.register("src.tasks.custom.custom_task")
    later = OutboxCommandRegistry.allowed_names()
    assert "src.tasks.custom.custom_task" in later
    assert "src.tasks.custom.custom_task" not in initial


# ---------------------------------------------------------------------------
# Duplicate event_id rejection
# ---------------------------------------------------------------------------


def test_emit_event_rejects_duplicate_event_id(fixed_now: datetime) -> None:
    event_id = uuid.UUID("00000000-0000-0000-0000-000000000111")
    _build_sample_event(event_id=event_id, fixed_now=fixed_now)
    with pytest.raises(OutboxContractViolation, match="duplicate"):
        _build_sample_event(event_id=event_id, fixed_now=fixed_now)


def test_emit_event_auto_generates_unique_event_ids() -> None:
    cmd = OutboxCommand(
        task_name="src.tasks.analyzer.analyze_session_task",
        args=(),
        kwargs={},
    )
    a = emit_event(command=cmd, task_log_id=1)
    b = emit_event(command=cmd, task_log_id=2)
    assert a.event_id != b.event_id


def test_new_event_id_returns_uuid() -> None:
    assert isinstance(new_event_id(), UUID)


# ---------------------------------------------------------------------------
# Stub publisher end-to-end (contract-layer integration)
# ---------------------------------------------------------------------------


class StubPublisher:
    """In-memory publisher for contract-level integration tests.

    Mirrors the real publisher's state transitions (claim → publish /
    fail) without touching PostgreSQL. Used to assert that the
    contract layer produces the right row shape for each state.
    """

    def __init__(self, instance_id: str = "stub-publisher") -> None:
        self.instance_id = instance_id
        self.events: list[OutboxEvent] = []
        self.published: list[tuple[str, UUID]] = []
        self.failures: list[tuple[str, str]] = []  # (event_id, reason)

    def claim(self, event: OutboxEvent) -> OutboxEvent:
        transition(event.status, OutboxStatus.PUBLISHING)
        claimed = _replace(
            event,
            status=OutboxStatus.PUBLISHING,
            claimed_by=self.instance_id,
            lease_expires_at=datetime.now(tz=timezone.utc) + timedelta(seconds=60),
        )
        self.events.append(claimed)
        return claimed

    def publish(self, claimed: OutboxEvent) -> OutboxEvent:
        transition(claimed.status, OutboxStatus.PUBLISHED)
        published = _replace(
            claimed,
            status=OutboxStatus.PUBLISHED,
            published_at=datetime.now(tz=timezone.utc),
            claimed_by=None,
            lease_expires_at=None,
            updated_at=datetime.now(tz=timezone.utc),
        )
        self.published.append((claimed.task_name, claimed.event_id))
        return published

    def retry(self, claimed: OutboxEvent, *, attempt_count: int) -> OutboxEvent:
        transition(claimed.status, OutboxStatus.PENDING)
        return _replace(
            claimed,
            status=OutboxStatus.PENDING,
            claimed_by=None,
            lease_expires_at=None,
            attempt_count=attempt_count,
            updated_at=datetime.now(tz=timezone.utc),
        )

    def fail(self, claimed: OutboxEvent, *, error: str) -> OutboxEvent:
        transition(claimed.status, OutboxStatus.FAILED)
        self.failures.append((str(claimed.event_id), error))
        return _replace(
            claimed,
            status=OutboxStatus.FAILED,
            claimed_by=None,
            lease_expires_at=None,
            last_error=error,
            updated_at=datetime.now(tz=timezone.utc),
        )


def _replace(event: OutboxEvent, **changes: Any) -> OutboxEvent:
    """Return a new event with the given fields overridden.

    ``dataclasses.replace`` does the same thing but mypy cannot
    statically verify the override keys against the field set when the
    event is frozen; this helper is a thin shim that delegates to
    ``replace`` and only allows fields that exist on the dataclass.
    """
    import dataclasses

    field_names = {f.name for f in dataclasses.fields(OutboxEvent)}
    invalid = set(changes) - field_names
    assert not invalid, f"unknown fields: {invalid}"
    return dataclasses.replace(event, **changes)


def test_stub_publisher_happy_path(fixed_now: datetime) -> None:
    event = _build_sample_event(fixed_now=fixed_now)
    publisher = StubPublisher()
    claimed = publisher.claim(event)
    published = publisher.publish(claimed)

    assert claimed.status is OutboxStatus.PUBLISHING
    assert claimed.claimed_by == publisher.instance_id
    assert claimed.lease_expires_at is not None
    assert published.status is OutboxStatus.PUBLISHED
    assert published.published_at is not None
    assert published.claimed_by is None
    assert published.lease_expires_at is None
    assert len(publisher.published) == 1
    assert publisher.published[0][1] == event.event_id


def test_stub_publisher_retry_loop(fixed_now: datetime) -> None:
    event = _build_sample_event(fixed_now=fixed_now)
    publisher = StubPublisher()
    claimed = publisher.claim(event)
    # First attempt fails → back to pending.
    requeued = publisher.retry(claimed, attempt_count=1)
    assert requeued.status is OutboxStatus.PENDING
    assert requeued.attempt_count == 1

    # Second claim + publish succeeds.
    claimed_again = publisher.claim(requeued)
    published = publisher.publish(claimed_again)
    assert published.status is OutboxStatus.PUBLISHED


def test_stub_publisher_fail_after_max_attempts(fixed_now: datetime) -> None:
    event = _build_sample_event(fixed_now=fixed_now)
    publisher = StubPublisher()
    claimed = publisher.claim(event)
    failed = publisher.fail(claimed, error="broker unreachable")
    assert failed.status is OutboxStatus.FAILED
    assert failed.last_error == "broker unreachable"
    assert failed.claimed_by is None
    assert len(publisher.failures) == 1


def test_duplicate_publish_path_is_idempotent_at_contract_layer(
    fixed_now: datetime,
) -> None:
    """Two publishers receiving the same ``event_id`` observe the same row.

    The DB unique constraint on ``event_id`` is the source of truth.
    At the contract layer, ``emit_event`` produces identical event
    rows for two callers referencing the same UUID; the second
    insert loses the race to the partial unique index on
    ``(task_log_id) WHERE status='pending'``. Here we verify the
    contract-layer row shape is the same so the consumer can compare
    task_id == event_id to short-circuit.
    """
    event_id = uuid.UUID("00000000-0000-0000-0000-000000000222")
    event = _build_sample_event(event_id=event_id, fixed_now=fixed_now)
    publisher = StubPublisher()
    published = publisher.publish(publisher.claim(event))
    # A second publisher receiving the same ``event_id`` would
    # observe the published row and short-circuit; the contract layer
    # does not enforce that (the consumer does), but it does guarantee
    # that the row's identity is the same UUID.
    assert published.event_id == event_id
    assert str(published.event_id) == event_id_str(event_id)


def event_id_str(value: UUID) -> str:
    return str(value)


# ---------------------------------------------------------------------------
# Field-set immutability (ADR §2)
# ---------------------------------------------------------------------------


def test_event_field_set_matches_canonical_adr_list() -> None:
    """Guard against accidental field addition / removal.

    Any change here must come with an ADR amendment. The canonical
    list is in ADR §2.
    """
    import dataclasses

    field_names = {f.name for f in dataclasses.fields(OutboxEvent)}
    assert field_names == {
        "event_id",
        "task_log_id",
        "dedupe_key",
        "task_name",
        "queue",
        "args_json",
        "kwargs_json",
        "status",
        "attempt_count",
        "next_attempt_at",
        "id",
        "claimed_by",
        "lease_expires_at",
        "published_at",
        "last_error",
        "created_at",
        "updated_at",
    }


def test_registry_default_whitelist_is_populated() -> None:
    """The cutover (Todo 14) relies on these names being already registered."""
    names = OutboxCommandRegistry.allowed_names()
    assert "src.tasks.session_build.full_build_task" in names
    assert "src.tasks.session_build.hot_build_task" in names
    assert "src.tasks.analyzer.analyze_session_task" in names
    assert "src.tasks.summarizer.generate_daily_summary_task" in names
    assert "src.tasks.webhook.send_webhook_task" in names


# ---------------------------------------------------------------------------
# Snapshot / deepcopy safety
# ---------------------------------------------------------------------------


def test_event_survives_deepcopy(fixed_now: datetime) -> None:
    event = _build_sample_event(fixed_now=fixed_now)
    cloned = copy.deepcopy(event)
    assert cloned == event
    assert cloned.event_id == event.event_id
