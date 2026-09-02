"""Outbox state machine.

States and transitions are documented in §3 of
``docs/adr/0011-transactional-outbox-and-task-lifecycle.md``. The
state machine is intentionally small (four states) and the
``ALLOWED_TRANSITIONS`` mapping is the single source of truth; the
:meth:`OutboxStatus.is_allowed` accessor on the enum is a convenience
that calls :func:`can_transition`.

The terminal states are :attr:`OutboxStatus.PUBLISHED` and
:attr:`OutboxStatus.FAILED`. There is **no** legal transition out of
``published``; ``failed`` only transitions back to ``pending`` via an
operator-initiated mgmt API call, not via the publisher loop.

.. important::

    The outbox is at-least-once, **not** exactly-once. Duplicate
    publishes are unavoidable; the consumer-side
    :func:`bind_or_create_running_task_log` short-circuit plus
    ``INSERT … ON CONFLICT (event_id) DO NOTHING`` business writes are
    the safety net.
"""

from __future__ import annotations

from enum import Enum
from typing import Iterable, Mapping

from src.application.outbox.errors import OutboxStateError


class OutboxStatus(str, Enum):
    """Lifecycle states for an outbox event.

    Stored in the ``outbox_event.status`` column as TEXT with a CHECK
    constraint ``IN ('pending','publishing','published','failed')``.
    The string values are stable; renaming any of them is an ADR
    amendment.
    """

    PENDING = "pending"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    FAILED = "failed"


#: Mapping of legal transitions. The set values are frozen; anything
#: not in the set raises :class:`OutboxStateError`. Operators
#: resetting a ``failed`` row to ``pending`` go through the mgmt API
#: (out of scope here); the publisher loop itself never emits
#: ``failed → pending``.
ALLOWED_TRANSITIONS: Mapping[OutboxStatus, frozenset[OutboxStatus]] = {
    OutboxStatus.PENDING: frozenset({OutboxStatus.PUBLISHING, OutboxStatus.FAILED}),
    OutboxStatus.PUBLISHING: frozenset(
        {
            OutboxStatus.PENDING,  # retryable failure / crash recovery
            OutboxStatus.PUBLISHED,  # broker accepted the message
            OutboxStatus.FAILED,  # attempt_count >= max_attempts
        }
    ),
    OutboxStatus.PUBLISHED: frozenset(),  # terminal
    OutboxStatus.FAILED: frozenset(
        {
            OutboxStatus.PENDING,  # manual operator reset only
        }
    ),
}

#: Terminal states. Any state in this set has no outgoing transitions
#: other than the manual ``failed → pending`` operator path.
TERMINAL_STATES: frozenset[OutboxStatus] = frozenset({OutboxStatus.PUBLISHED, OutboxStatus.FAILED})


def can_transition(current: OutboxStatus, target: OutboxStatus) -> bool:
    """Return True if ``current → target`` is in :data:`ALLOWED_TRANSITIONS`."""
    return target in ALLOWED_TRANSITIONS.get(current, frozenset())


def is_terminal(state: OutboxStatus) -> bool:
    """Return True if ``state`` is terminal.

    ``PUBLISHED`` is permanently terminal. ``FAILED`` is *operationally*
    terminal (the publisher will not retry it on its own) but the
    contract still permits ``failed → pending`` for the operator reset
    path; callers that want to treat ``failed`` as fully terminal
    should check the state explicitly rather than rely on this helper.
    """
    return state == OutboxStatus.PUBLISHED


def transition(
    current: OutboxStatus,
    target: OutboxStatus,
    *,
    expected_from: Iterable[OutboxStatus] | None = None,
) -> OutboxStatus:
    """Validate a transition and return ``target``.

    ``expected_from`` is the caller's assertion about what state the
    row is in **right now** (e.g. the publisher's ``UPDATE … WHERE
    status='publishing'``). The helper still consults
    :data:`ALLOWED_TRANSITIONS` for the final check; ``expected_from``
    is purely a guard against the caller passing an inconsistent
    pair (``expected_from=PUBLISHED`` while claiming ``current=PENDING``).

    Raises :class:`OutboxStateError` when:
    - the transition is not in :data:`ALLOWED_TRANSITIONS`;
    - ``expected_from`` is provided and ``current`` is not in it.
    """
    if expected_from is not None and current not in frozenset(expected_from):
        raise OutboxStateError(
            f"outbox transition {current.value} → {target.value} "
            f"violates expected_from={sorted(s.value for s in expected_from)}",
            payload={
                "current": current.value,
                "target": target.value,
                "expected_from": sorted(s.value for s in expected_from),
            },
        )
    if not can_transition(current, target):
        raise OutboxStateError(
            f"outbox transition {current.value} → {target.value} is not allowed",
            payload={"current": current.value, "target": target.value},
        )
    return target


__all__ = [
    "ALLOWED_TRANSITIONS",
    "OutboxStatus",
    "TERMINAL_STATES",
    "can_transition",
    "is_terminal",
    "transition",
]
