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
from src.services.analysis.ports import AnalysisPorts, build_analysis_ports

# Module-level composition-root singleton. Mirrors ``src/api/deps.py`` and
# ``src/mcp/tools.py`` so all three entry points share one wiring pattern.
_container: Container = bootstrap_production()

# Module-level analyzer-pipeline ports holder. The task layer reads the
# :class:`~src.services.analysis.ports.AnalysisPorts` bundle through
# :func:`get_analysis_ports`; production code paths take the ``None``
# default
# (lazy-resolved via :func:`build_analysis_ports`) and tests inject a
# scripted fake through :func:`set_analysis_ports_for_tests`. The holder is
# ``None`` until first access — keeping the module load-time graph free of
# the underlying service / aggregator imports.
_analysis_ports: AnalysisPorts | None = None


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


def get_analysis_ports() -> AnalysisPorts:
    """Return the analyzer-pipeline ports bundle for Celery task modules.

    Resolves to a fresh :func:`build_analysis_ports` instance on every
    cold call, then caches the result so repeated invocations within a
    single worker process pay no extra import cost.
    """

    global _analysis_ports
    if _analysis_ports is None:
        _analysis_ports = build_analysis_ports()
    return _analysis_ports


def set_analysis_ports_for_tests(ports: AnalysisPorts) -> None:
    """Replace the analyzer-pipeline ports holder (test-only escape hatch).

    Mirrors :func:`set_container_for_tests`: production code never calls
    this; tests use it so the analyzer orchestration runs against a
    scripted :class:`AnalysisPorts` instead of monkey-patching the
    individual helper names in :mod:`src.tasks._analyzer_orchestration`.
    """

    global _analysis_ports
    _analysis_ports = ports


def reset_analysis_ports_for_tests() -> None:
    """Restore the analyzer-pipeline ports holder to ``None``.

    The next call to :func:`get_analysis_ports` will rebuild the
    production wiring via :func:`build_analysis_ports`.
    """

    global _analysis_ports
    _analysis_ports = None


__all__ = [
    "get_analysis_ports",
    "get_container",
    "reset_analysis_ports_for_tests",
    "reset_container_for_tests",
    "set_analysis_ports_for_tests",
    "set_container_for_tests",
]
