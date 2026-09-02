"""Task control port for the application composition root.

This port abstracts the broker- and worker-level control operations the
API and maintenance layers need (task cancel / revoke, worker heartbeat
inspection). The Celery ``app.control`` namespace is a runtime side effect
that must not leak across the application boundary, so we expose a
protocol shape here and let ``bootstrap_production()`` bind the
Celery-backed adapter.

Use cases and endpoints depend on ``TaskControlPort`` instead of
importing :mod:`src.core.celery_app` directly. Tests substitute a
recording fake to assert the right Celery ``task_id`` was revoked
without ever opening a Redis connection.

See ``.omo/plans/architecture-consolidation.md`` Todo 5 — composition
root — and Todo 7 (which will inject a Celery-backed implementation
into Celery task modules).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol, runtime_checkable


@dataclass(frozen=True)
class HeartbeatReport:
    """Snapshot of broker / worker liveness for a given Celery queue.

    Attributes:
        queue: Name of the queue inspected (``"celery"``,
            ``"analysis_hot"``, ``"analysis_full"``, ...). May be empty
            for a broker-wide heartbeat.
        worker_count: Number of workers currently subscribed to the
            queue. ``None`` if the broker does not support per-queue
            worker inspection (e.g. Redis without ``CELERY_TRACK_STARTED``).
        active_count: Number of currently-executing tasks on the queue.
            ``None`` when unsupported.
    """

    queue: str
    worker_count: Optional[int]
    active_count: Optional[int]


@runtime_checkable
class TaskControlPort(Protocol):
    """Abstract Celery-side task control surface.

    Operations are intentionally coarse-grained and best-effort: the
    production Celery adapter translates each call into one
    ``app.control`` round-trip and swallows / reports broker errors.
    Tests can implement these as no-ops with a recorded call log.
    """

    def revoke(self, task_id: str, *, terminate: bool = False) -> None:
        """Revoke a queued or running task by Celery ``task_id``.

        Args:
            task_id: Celery task identifier (UUID returned when the task
                was originally enqueued).
            terminate: When ``True`` the worker is asked to send
                ``TERM`` to the running task so it is killed instead of
                merely dequeued. Mirrors
                ``celery_app.control.revoke(task_id, terminate=...)``.
        """
        ...

    def heartbeat(self, queue: str = "") -> HeartbeatReport:
        """Return a broker/worker heartbeat report for ``queue``.

        ``queue`` may be empty to ask for a broker-wide summary. The
        production adapter wraps ``celery_app.control.inspect()`` calls
        and tolerates ``None`` replies (broker down / no workers).
        """
        ...


__all__ = ["HeartbeatReport", "TaskControlPort"]
