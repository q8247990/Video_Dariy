"""Outbox state machine tests (ADR 0011 §3).

These tests lock the transition table:

- every entry in :data:`ALLOWED_TRANSITIONS` succeeds;
- every other transition raises :class:`OutboxStateError`;
- :func:`is_terminal` correctly classifies ``PUBLISHED`` as terminal
  and the other states as non-terminal;
- :func:`transition` honors the ``expected_from`` guard when provided.
"""

from __future__ import annotations

import pytest

from src.application.outbox.errors import OutboxStateError
from src.application.outbox.state_machine import (
    ALLOWED_TRANSITIONS,
    TERMINAL_STATES,
    OutboxStatus,
    can_transition,
    is_terminal,
    transition,
)

# ---------------------------------------------------------------------------
# Allowed transitions
# ---------------------------------------------------------------------------


def test_allowed_transitions_table_matches_adr() -> None:
    """The transition table is the canonical contract.

    Frozen dataclass-style: any change here must be reflected in the
    ADR (§3) before the table is amended.
    """
    assert ALLOWED_TRANSITIONS[OutboxStatus.PENDING] == frozenset(
        {OutboxStatus.PUBLISHING, OutboxStatus.FAILED}
    )
    assert ALLOWED_TRANSITIONS[OutboxStatus.PUBLISHING] == frozenset(
        {
            OutboxStatus.PENDING,
            OutboxStatus.PUBLISHED,
            OutboxStatus.FAILED,
        }
    )
    assert ALLOWED_TRANSITIONS[OutboxStatus.PUBLISHED] == frozenset()
    assert ALLOWED_TRANSITIONS[OutboxStatus.FAILED] == frozenset({OutboxStatus.PENDING})


def test_terminal_states_contains_published_and_failed() -> None:
    assert TERMINAL_STATES == frozenset({OutboxStatus.PUBLISHED, OutboxStatus.FAILED})


def test_terminal_helper_only_treats_published_as_terminal() -> None:
    """``failed`` is operationally terminal but the contract permits a manual reset."""
    assert is_terminal(OutboxStatus.PUBLISHED) is True
    assert is_terminal(OutboxStatus.PENDING) is False
    assert is_terminal(OutboxStatus.PUBLISHING) is False
    # ``failed`` is operationally terminal but the contract still
    # permits ``failed -> pending`` via the mgmt API.
    assert is_terminal(OutboxStatus.FAILED) is False


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (OutboxStatus.PENDING, OutboxStatus.PUBLISHING),
        (OutboxStatus.PENDING, OutboxStatus.FAILED),
        (OutboxStatus.PUBLISHING, OutboxStatus.PENDING),
        (OutboxStatus.PUBLISHING, OutboxStatus.PUBLISHED),
        (OutboxStatus.PUBLISHING, OutboxStatus.FAILED),
        (OutboxStatus.FAILED, OutboxStatus.PENDING),
    ],
)
def test_legal_transitions_succeed(current: OutboxStatus, target: OutboxStatus) -> None:
    assert can_transition(current, target) is True
    assert transition(current, target) is target


# ---------------------------------------------------------------------------
# Forbidden transitions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("current", "target"),
    [
        # Skip publishing (publisher must go through it)
        (OutboxStatus.PENDING, OutboxStatus.PUBLISHED),
        # Published is permanently terminal
        (OutboxStatus.PUBLISHED, OutboxStatus.PENDING),
        (OutboxStatus.PUBLISHED, OutboxStatus.PUBLISHING),
        (OutboxStatus.PUBLISHED, OutboxStatus.FAILED),
        # Failed must be reset to pending, not to publishing
        (OutboxStatus.FAILED, OutboxStatus.PUBLISHING),
        (OutboxStatus.FAILED, OutboxStatus.PUBLISHED),
        # Self-loops on a non-terminal state are forbidden
        (OutboxStatus.PENDING, OutboxStatus.PENDING),
        (OutboxStatus.PUBLISHING, OutboxStatus.PUBLISHING),
    ],
)
def test_illegal_transitions_raise_state_error(current: OutboxStatus, target: OutboxStatus) -> None:
    assert can_transition(current, target) is False
    with pytest.raises(OutboxStateError) as excinfo:
        transition(current, target)
    payload = excinfo.value.payload
    assert payload == {"current": current.value, "target": target.value}


# ---------------------------------------------------------------------------
# expected_from guard
# ---------------------------------------------------------------------------


def test_expected_from_guard_rejects_mismatched_current() -> None:
    """When the caller asserts ``expected_from``, mismatches must raise."""
    with pytest.raises(OutboxStateError) as excinfo:
        transition(
            OutboxStatus.PENDING,
            OutboxStatus.PUBLISHING,
            expected_from={OutboxStatus.PUBLISHING},
        )
    payload = excinfo.value.payload
    assert payload == {
        "current": "pending",
        "target": "publishing",
        "expected_from": ["publishing"],
    }


def test_expected_from_guard_accepts_matching_current() -> None:
    result = transition(
        OutboxStatus.PENDING,
        OutboxStatus.PUBLISHING,
        expected_from={OutboxStatus.PENDING, OutboxStatus.FAILED},
    )
    assert result is OutboxStatus.PUBLISHING


def test_expected_from_guard_does_not_relax_allowed_transitions() -> None:
    """``expected_from`` is a guard, not a relaxation: ``pending ->
    published`` is still forbidden."""
    with pytest.raises(OutboxStateError):
        transition(
            OutboxStatus.PENDING,
            OutboxStatus.PUBLISHED,
            expected_from={OutboxStatus.PENDING},
        )
