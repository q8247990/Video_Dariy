"""Outbox contract types and helpers.

This package freezes the **contract** that the application-level
outbox relies on. The persistence model (Todo 12) and the publisher
process (Todo 13) consume this contract without standing up a database.

The contract is at-least-once. Consumers MUST be idempotent; the
broker ``task_id`` is fixed to ``event_id`` so duplicate publishes
short-circuit at the worker via the existing
``bind_or_create_running_task_log`` helper. See ADR
``docs/adr/0011-transactional-outbox-and-task-lifecycle.md`` for the
full contract.

Submodules
==========

``errors``
    Exception hierarchy (base :class:`OutboxError` and concrete
    subclasses for state, payload, contract and registry violations).

``payload_validator``
    JSON-safe payload validation (rejecting ``bytes`` / ``set`` /
    arbitrary objects / oversize structures; allowing ``UUID``,
    ``datetime``, ``date``, tuple-as-list, ``None``, primitives).

``registry``
    Whitelist of Celery ``task_name`` values that the outbox is
    allowed to publish. Populated with the four task names the current
    ``CeleryTaskDispatcher`` already targets so the cutover (Todo 14)
    does not need to register anything new.

``contracts``
    Frozen dataclasses for :class:`OutboxCommand` (the in-memory
    payload that the application builds) and :class:`OutboxEvent` (the
    durable row, including the field list documented in §2 of the ADR)
    plus ``to_row_dict`` / ``from_row_dict`` helpers used by the
    persistence model and the contract tests.

``state_machine``
    :class:`OutboxStatus` enum, the ``ALLOWED_TRANSITIONS`` mapping
    and the :func:`transition` / :func:`can_transition` /
    :func:`is_terminal` helpers.

``enqueue``
    Thin orchestration helper :func:`enqueue_command` that wraps
    :class:`OutboxRepository` and is the public entry point for
    application use cases. The repository itself is exposed as
    :class:`OutboxRepository` for the persistence tests; the use case
    is the documented entry point for production code.

``repository``
    SQLAlchemy bridge for the ``outbox_event`` table. Owns the
    ``enroll_with_task_log`` atomic-enqueue path, the publisher claim
    loop (``try_claim_one`` with ``FOR UPDATE SKIP LOCKED``), the
    state-machine updates (``mark_published`` /
    ``mark_failed_to_retry`` / ``mark_failed_terminal``), and the
    retention / supervision helpers. The contract layer (Todo 11) is
    the in-memory shape; this module is the persistence bridge.

``publisher``
    The standalone publisher process (Todo 13). Owns the
    claim → ``broker.send_task`` → ``mark_published`` /
    ``mark_failed_to_retry`` / ``mark_failed_terminal`` loop, the
    :class:`BrokerPort` Protocol, the production
    :class:`CeleryBrokerPort` adapter, the broker-exception
    classification (:func:`classify_celery_error`), the
    backoff math (:func:`compute_next_attempt_at`), and the
    :class:`PublisherConfig` / :class:`PublisherStats` /
    :class:`PublisherHealth` dataclasses.

``cli``
    The ``python -m src.application.outbox`` entry point. Owns the
    ``SessionLocal`` lifecycle, the SIGTERM / SIGINT stop flag, and
    the optional ``--stats-file`` JSON snapshot. The
    ``outbox-publisher`` supervisord program inside the backend
    container runs this CLI; the ``--once`` flag is the test entry point.

``__main__``
    Re-exports :func:`src.application.outbox.cli.main` so
    ``python -m src.application.outbox`` resolves to the CLI without
    requiring ``python -m src.application.outbox.cli``.
"""

from __future__ import annotations

from src.application.outbox.cli import main
from src.application.outbox.contracts import (
    DEFAULT_MAX_PAYLOAD_BYTES,
    DEFAULT_MAX_PAYLOAD_DEPTH,
    JSONSafeValue,
    OutboxCommand,
    OutboxEvent,
    OutboxRowDict,
    emit_event,
    new_event_id,
)
from src.application.outbox.enqueue import enqueue_command
from src.application.outbox.errors import (
    OutboxContractViolation,
    OutboxError,
    OutboxPayloadError,
    OutboxRegistryError,
    OutboxStateError,
)
from src.application.outbox.payload_validator import (
    JSON_SAFE_ATOMIC_TYPES,
    JSON_SAFE_CONTAINER_TYPES,
    ensure_json_safe,
    is_json_safe,
    validate_command_payload,
)
from src.application.outbox.publisher import (
    PUBLISHER_BACKLOG_DEGRADED_SECONDS,
    PUBLISHER_BACKOFF_BASE_SECONDS,
    PUBLISHER_BACKOFF_CAP_SECONDS,
    PUBLISHER_BATCH_SIZE,
    PUBLISHER_LEASE_SECONDS,
    PUBLISHER_MAX_ATTEMPTS,
    PUBLISHER_POLL_INTERVAL_SECONDS,
    PUBLISHER_PUBLISHED_RETENTION_DAYS,
    BrokerPort,
    CeleryBrokerPort,
    OutboxPublisher,
    PublisherConfig,
    PublisherHealth,
    PublisherStats,
    classify_celery_error,
    compute_next_attempt_at,
)
from src.application.outbox.registry import (
    DEFAULT_QUEUE,
    OutboxCommandRegistry,
)
from src.application.outbox.repository import EnrollOutcome, OutboxRepository
from src.application.outbox.state_machine import (
    ALLOWED_TRANSITIONS,
    TERMINAL_STATES,
    OutboxStatus,
    can_transition,
    is_terminal,
    transition,
)

__all__ = [
    "ALLOWED_TRANSITIONS",
    "BrokerPort",
    "CeleryBrokerPort",
    "DEFAULT_MAX_PAYLOAD_BYTES",
    "DEFAULT_MAX_PAYLOAD_DEPTH",
    "DEFAULT_QUEUE",
    "EnrollOutcome",
    "JSONSafeValue",
    "JSON_SAFE_ATOMIC_TYPES",
    "JSON_SAFE_CONTAINER_TYPES",
    "OutboxCommand",
    "OutboxCommandRegistry",
    "OutboxContractViolation",
    "OutboxError",
    "OutboxEvent",
    "OutboxPayloadError",
    "OutboxPublisher",
    "OutboxRegistryError",
    "OutboxRepository",
    "OutboxRowDict",
    "OutboxStateError",
    "OutboxStatus",
    "PUBLISHER_BACKLOG_DEGRADED_SECONDS",
    "PUBLISHER_BACKOFF_BASE_SECONDS",
    "PUBLISHER_BACKOFF_CAP_SECONDS",
    "PUBLISHER_BATCH_SIZE",
    "PUBLISHER_LEASE_SECONDS",
    "PUBLISHER_MAX_ATTEMPTS",
    "PUBLISHER_POLL_INTERVAL_SECONDS",
    "PUBLISHER_PUBLISHED_RETENTION_DAYS",
    "PublisherConfig",
    "PublisherHealth",
    "PublisherStats",
    "TERMINAL_STATES",
    "can_transition",
    "classify_celery_error",
    "compute_next_attempt_at",
    "emit_event",
    "enqueue_command",
    "ensure_json_safe",
    "is_json_safe",
    "is_terminal",
    "main",
    "new_event_id",
    "transition",
    "validate_command_payload",
]
