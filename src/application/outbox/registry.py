"""Outbox task-name registry.

The registry is the **whitelist** of Celery ``task_name`` values the
outbox is allowed to publish. It is intentionally process-local: it
is a static declaration of "what the application is willing to
publish", not a configuration knob. A task present at runtime but
absent from the registry is a contract violation, surfaced at emit
time as :class:`OutboxRegistryError`.

Why a whitelist?

- ADR §6 forbids publishing commands whose ``task_name`` is unknown.
  The registry is the structural enforcement; the only way to extend
  the publishable surface is to add an entry here in the same commit
  that adds the task module.
- The registry doubles as a queue hint. ``queue_for(task_name)`` is
  the canonical mapping; the Celery ``queue`` argument on
  :class:`OutboxCommand` is allowed to override it (analyzer picks
  ``analysis_hot`` / ``analysis_full`` at the call site), but a
  caller cannot smuggle a queue into the broker without first
  referencing a registered task.
"""

from __future__ import annotations

from typing import Iterable, Mapping

from src.application.outbox.errors import OutboxRegistryError

#: Default Celery queue used when a registered task does not specify
#: one and the caller did not override it at emit time.
DEFAULT_QUEUE: str = "celery"


class OutboxCommandRegistry:
    """Process-local whitelist of publishable Celery task names.

    The class is intentionally a namespace (classmethods only). It is
    not a singleton object because the test suite instantiates a
    fresh registration set per test (the underlying class-level
    storage is replaced by calling :meth:`reset_for_testing`).
    """

    _registry: Mapping[str, str] = {
        # Populated with the four Celery task names that
        # ``CeleryTaskDispatcher`` already targets. The cutover
        # (Todo 14) does not need to add new entries; the registry
        # intentionally starts populated so callers do not have to
        # re-declare what the application already publishes.
        "src.tasks.session_build.full_build_task": DEFAULT_QUEUE,
        "src.tasks.session_build.hot_build_task": DEFAULT_QUEUE,
        "src.tasks.analyzer.analyze_session_task": DEFAULT_QUEUE,
        "src.tasks.summarizer.generate_daily_summary_task": DEFAULT_QUEUE,
        "src.tasks.webhook.send_webhook_task": DEFAULT_QUEUE,
    }

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    @classmethod
    def register(cls, task_name: str, queue: str = DEFAULT_QUEUE) -> None:
        """Register ``task_name`` with the default Celery ``queue``.

        Idempotent: re-registering an existing name updates the queue
        hint. The contract layer does not call this; it is meant for
        task-module authors who add a brand-new Celery task and want
        the outbox to be allowed to publish it.
        """
        cls._registry = {**cls._registry, task_name: queue}

    @classmethod
    def unregister(cls, task_name: str) -> None:
        """Remove a task name from the whitelist.

        Used by tests only. Production code MUST NOT call this; the
        whitelist is meant to grow with the application, not shrink
        at runtime.
        """
        cls._registry = {name: queue for name, queue in cls._registry.items() if name != task_name}

    @classmethod
    def reset_for_testing(cls) -> None:
        """Replace the registry with the default whitelist.

        Tests that need an isolated whitelist call this in their
        fixture's ``setup`` / ``teardown`` so one test cannot leak
        names into another.
        """
        cls._registry = {
            "src.tasks.session_build.full_build_task": DEFAULT_QUEUE,
            "src.tasks.session_build.hot_build_task": DEFAULT_QUEUE,
            "src.tasks.analyzer.analyze_session_task": DEFAULT_QUEUE,
            "src.tasks.summarizer.generate_daily_summary_task": DEFAULT_QUEUE,
            "src.tasks.webhook.send_webhook_task": DEFAULT_QUEUE,
        }

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------

    @classmethod
    def is_allowed(cls, task_name: str) -> bool:
        """Return True if ``task_name`` may be published via the outbox."""
        return task_name in cls._registry

    @classmethod
    def queue_for(cls, task_name: str) -> str:
        """Return the registered default queue for ``task_name``.

        Raises :class:`OutboxRegistryError` if the task name is not
        registered; callers that want to skip the check and use a
        custom queue should pass ``queue=`` explicitly when emitting
        the command and only call :meth:`is_allowed` themselves.
        """
        if task_name not in cls._registry:
            raise OutboxRegistryError(
                f"task_name {task_name!r} is not in the outbox registry whitelist",
                payload=task_name,
            )
        return cls._registry[task_name]

    @classmethod
    def allowed_names(cls) -> frozenset[str]:
        """Return a snapshot of all registered task names.

        Used by tests to assert the whitelist membership rules; not
        used by the hot path.
        """
        return frozenset(cls._registry.keys())

    @classmethod
    def all_entries(cls) -> Iterable[tuple[str, str]]:
        """Iterate over ``(task_name, queue)`` pairs.

        Order is not stable across Python versions for ``dict``
        iteration; consumers that need order should sort the result
        themselves.
        """
        return tuple(cls._registry.items())


__all__ = ["DEFAULT_QUEUE", "OutboxCommandRegistry"]
