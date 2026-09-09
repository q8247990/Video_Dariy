"""Unit test for the event_id -> worker-log correlation wiring.

The outbox publisher sends every Celery message with
``task_id = OutboxEvent.event_id`` (ADR §7), and
:mod:`src.core.celery_app` registers a ``task_prerun`` signal that copies
``self.request.id`` into the correlation ContextVar. This test fires that
signal to prove the worker-side chain: a single ``event_id`` becomes the
``correlation_id`` on the task's structured log lines, matching the same
id the API dispatch log and the publisher log already carry.

Importing :mod:`src.core.celery_app` triggers the signal-registration
side effect at module load time (see :func:`_wire_correlation_signals`),
so the tests do not need to reach into the private helper — they fire
the registered ``task_prerun`` / ``task_postrun`` signals directly and
assert on the public :func:`get_correlation_id` boundary.
"""

from __future__ import annotations

from celery.signals import task_postrun, task_prerun

from src.core import celery_app  # noqa: F401  (side effect: wires signals)
from src.core.logging_config import (
    CORRELATION_CONTEXT,
    get_correlation_id,
    set_correlation_id,
)


def test_task_prerun_signal_sets_correlation_from_task_id() -> None:
    set_correlation_id(None)
    task_prerun.send(sender=None, task_id="evt-abc-123")
    try:
        assert get_correlation_id() == "evt-abc-123"
    finally:
        task_postrun.send(sender=None)
        assert get_correlation_id() is None


def test_task_prerun_with_empty_task_id_leaves_correlation_none() -> None:
    set_correlation_id(None)
    task_prerun.send(sender=None, task_id="")
    assert get_correlation_id() is None
    task_postrun.send(sender=None)
    assert CORRELATION_CONTEXT.get() is None
