"""Composition-root container holder for the Celery task layer (Todo 7).

``src/tasks/*`` used to construct :class:`CeleryTaskDispatcher` and
:class:`OpenAICompatGatewayFactory` inline, which bound the task layer to
``src.infrastructure.*`` and made hermetic unit tests dependent on
monkeypatching concrete adapter symbols. This module is the task-layer
equivalent of ``src.api.deps._container`` / ``src.mcp.tools._container``:

* the production :class:`~src.application.bootstrap.Container` is built
  **once** at module import time via
  :func:`~src.application.bootstrap.bootstrap_production`;
* task functions read the ports they need through :func:`get_container`;
* tests substitute a fake-bound container with
  :func:`set_container_for_tests` (and restore with
  :func:`reset_container_for_tests`), so dispatch can be asserted without
  a Redis broker or a live LLM endpoint.

Building the container is cheap and side-effect free apart from importing
``src.core.celery_app`` (which every task module already does), so the
eager module-level binding keeps behaviour identical to the previous
``CeleryTaskDispatcher()`` construction while removing the direct
infrastructure dependency.
"""

from __future__ import annotations

from src.application.bootstrap import Container, bootstrap_production

# Module-level composition-root singleton. Mirrors ``src/api/deps.py`` and
# ``src/mcp/tools.py`` so all three entry points share one wiring pattern.
_container: Container = bootstrap_production()


def get_container() -> Container:
    """Return the composition-root container for Celery task modules."""

    return _container


def set_container_for_tests(container: Container) -> None:
    """Replace the module-level container (test-only escape hatch).

    Production code never calls this. Tests use it (usually via a
    ``bootstrap_for_tests(...)`` result) so task functions dispatch into
    :class:`~src.application.bootstrap_fakes.FakeTaskDispatcher` instead
    of Celery.
    """

    global _container
    _container = container


def reset_container_for_tests() -> None:
    """Restore the production binding after :func:`set_container_for_tests`."""

    global _container
    _container = bootstrap_production()


__all__ = [
    "get_container",
    "reset_container_for_tests",
    "set_container_for_tests",
]
