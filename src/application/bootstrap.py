"""Application composition root.

This module is the **single** place in the codebase allowed to import
``src.infrastructure.*`` adapters for the purpose of binding them to
``src.application.ports.*`` Protocols. Every concrete
``CeleryTaskDispatcher`` / ``OpenAICompatGatewayFactory`` /
``celery_app.control`` touch-point must be reached through one of the
two factories exposed here:

* :func:`bootstrap_production` — wires the real adapters behind the
  composition ``Container``. Use it from ``src.main`` /
  ``src.tasks`` / ``src.mcp`` boot paths.
* :func:`bootstrap_for_tests` — wires in-memory fakes by default and
  accepts optional keyword overrides. Use it from ``tests/`` and
  from any use case that wants a hermetic container.

Why a dedicated module:

* Use cases (``src/application/...``) depend only on ports, never on
  adapters or Celery objects directly.
* ``src/api``, ``src/mcp`` and ``src/tasks`` switch from hard-coded
  ``CeleryTaskDispatcher()`` / ``OpenAICompatGatewayFactory()`` to a
  single ``Container`` injection point — a precondition for Wave 1
  Todos 6 & 7.
* Tests build ``Container`` instances without monkeypatching
  ``src.infrastructure`` or any global Celery state.

The composition root also binds the
:class:`~src.application.system_config.LocaleProvider` that
``src.core.i18n.get_system_default_locale`` delegates to — under Todo 8
the i18n module no longer touches ``src.db.session`` directly.

Architectural rules (enforced by ``tests/architecture/test_dependency_boundaries.py``):

* Only this module is allowed to ``import src.infrastructure.*``.
* The composition root never owns SQLAlchemy ``Session`` instances or
  stateful gateways — those have request/transaction or per-call
  lifetimes and are constructed by the caller / use case.
* Defaults (``DEFAULT_ADMIN_PASSWORD`` behaviour, empty ``MCP_TOKEN``,
  CORS ``allow_origins``, signed media, UTC storage, provider key
  encryption, FK deletion policy) are **not** touched here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from src.application.bootstrap_errors import ConfigurationError
from src.application.bootstrap_fakes import (
    FakeClock,
    FakeIdGenerator,
    FakeLLMGatewayFactory,
    FakeTaskControl,
    FakeTaskDispatcher,
)
from src.application.ports.clock import ClockPort, IdGeneratorPort
from src.application.ports.llm_gateway import LLMGatewayFactoryPort
from src.application.ports.task_control import HeartbeatReport, TaskControlPort
from src.application.ports.task_dispatcher import TaskDispatcherPort

# ---------------------------------------------------------------------------
# Container
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Container:
    """The composition root's public surface.

    Every field is a port ``Protocol``-typed binding. Use cases consume
    the container by attribute access; they must never reach into the
    fakes or production adapter modules directly.

    Attributes:
        dispatcher: Async task dispatcher (Celery in production, fake in
            tests). Routes session build / analyzer / summary / webhook
            commands into the broker (or in-memory list, under tests).
        llm_factory: LLM gateway factory. ``build(...)`` returns a fresh
            gateway per provider; the factory itself is stateless.
        clock: Wall-clock source. Production returns timezone-aware UTC
            ``datetime``; tests substitute a deterministic
            :class:`FakeClock`.
        id_gen: Identifier generator. Production returns ``uuid.uuid4``
            hex; tests return ``"fake-id-N"`` or seeded UUIDs.
        task_control: Celery ``app.control`` surface — revoke / heartbeat.
            Always present so endpoints can assert broker state without
            importing ``src.core.celery_app`` directly.
    """

    dispatcher: TaskDispatcherPort
    llm_factory: LLMGatewayFactoryPort
    clock: ClockPort
    id_gen: IdGeneratorPort
    task_control: TaskControlPort


# ---------------------------------------------------------------------------
# Production adapters
# ---------------------------------------------------------------------------


class _SystemClock(ClockPort):
    """Wall-clock adapter — returns timezone-aware UTC."""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class _UuidIdGenerator(IdGeneratorPort):
    """UUIDv4 adapter — opaque, collision-resistant in production."""

    def new_id(self) -> str:
        return str(uuid4())


class _CeleryTaskControl(TaskControlPort):
    """Adapter for Celery ``app.control`` revoke / heartbeat.

    This is the only class in ``src.application.*`` allowed to call
    :func:`celery_app.control.revoke` directly. The Celery import is
    deferred to ``__init__`` so that tests that never reach the
    production binding never touch Redis.

    The adapter follows the existing endpoint behaviour:

    * ``revoke(task_id, terminate=...)`` mirrors
      ``celery_app.control.revoke`` and surfaces broker errors as
      ``RuntimeError`` (the endpoint already maps these to HTTP 5001).
    * ``heartbeat(queue)`` uses ``celery_app.control.inspect()`` and
      tolerates ``None`` replies (broker down / no workers); the
      counts come back as ``None`` in that case.
    """

    def __init__(self) -> None:
        # Imported lazily to keep unit tests free of Celery state until
        # they explicitly ask for the production binding.
        from src.core.celery_app import celery_app

        self._celery_app = celery_app

    def revoke(self, task_id: str, *, terminate: bool = False) -> None:
        self._celery_app.control.revoke(task_id, terminate=terminate)

    def heartbeat(self, queue: str = "") -> HeartbeatReport:
        destination = [queue] if queue else None
        try:
            ping = self._celery_app.control.inspect().ping(destination=destination)
        except Exception:
            ping = None

        try:
            active = self._celery_app.control.inspect().active(destination=destination)
        except Exception:
            active = None

        worker_count: Optional[int] = None
        active_count: Optional[int] = None

        if ping:
            worker_count = sum(len(v) for v in ping.values() if isinstance(v, list))
        if active:
            active_count = sum(len(v) for v in active.values() if isinstance(v, list))

        return HeartbeatReport(
            queue=queue,
            worker_count=worker_count,
            active_count=active_count,
        )


# ---------------------------------------------------------------------------
# Public factory: bootstrap_production
# ---------------------------------------------------------------------------


def bootstrap_production() -> Container:
    """Wire production adapters into a :class:`Container`.

    Importing this function does **not** open a Redis connection or
    create a Celery app — the only I/O happens when use cases actually
    call into the bound ports. The function is safe to invoke from
    application startup; the Celery ``app.control`` adapter is the
    only one that touches a broker at construction time (and only to
    import the module-level ``celery_app``).
    """
    from src.infrastructure.llm.openai_gateway import OpenAICompatGatewayFactory
    from src.infrastructure.tasks.celery_dispatcher import CeleryTaskDispatcher

    _install_production_locale_provider()

    return Container(
        dispatcher=CeleryTaskDispatcher(),
        llm_factory=OpenAICompatGatewayFactory(),
        clock=_SystemClock(),
        id_gen=_UuidIdGenerator(),
        task_control=_CeleryTaskControl(),
    )


# ---------------------------------------------------------------------------
# Public factory: bootstrap_for_tests
# ---------------------------------------------------------------------------


def bootstrap_for_tests(
    *,
    dispatcher: Optional[TaskDispatcherPort] = None,
    llm_factory: Optional[LLMGatewayFactoryPort] = None,
    clock: Optional[ClockPort] = None,
    id_gen: Optional[IdGeneratorPort] = None,
    task_control: Optional[TaskControlPort] = None,
) -> Container:
    """Build a hermetic :class:`Container` for tests.

    Every binding accepts an explicit override; missing bindings fall
    back to the corresponding fake from
    :mod:`src.application.bootstrap_fakes`. Any override that does not
    satisfy the corresponding port ``Protocol`` raises
    :class:`ConfigurationError` so test setup failures surface clearly
    instead of producing a runtime ``AttributeError`` later.
    """

    resolved_dispatcher: TaskDispatcherPort = dispatcher or FakeTaskDispatcher()
    _verify_port(
        resolved_dispatcher,
        TaskDispatcherPort,
        binding="dispatcher",
    )

    resolved_llm_factory: LLMGatewayFactoryPort = llm_factory or FakeLLMGatewayFactory()
    _verify_port(
        resolved_llm_factory,
        LLMGatewayFactoryPort,
        binding="llm_factory",
    )

    resolved_clock: ClockPort = clock or FakeClock()
    _verify_port(
        resolved_clock,
        ClockPort,
        binding="clock",
    )

    resolved_id_gen: IdGeneratorPort = id_gen or FakeIdGenerator()
    _verify_port(
        resolved_id_gen,
        IdGeneratorPort,
        binding="id_gen",
    )

    resolved_task_control: TaskControlPort = task_control or FakeTaskControl()
    _verify_port(
        resolved_task_control,
        TaskControlPort,
        binding="task_control",
    )

    install_test_locale_provider()

    return Container(
        dispatcher=resolved_dispatcher,
        llm_factory=resolved_llm_factory,
        clock=resolved_clock,
        id_gen=resolved_id_gen,
        task_control=resolved_task_control,
    )


# ---------------------------------------------------------------------------
# Locale provider binding
# ---------------------------------------------------------------------------


def _install_production_locale_provider() -> None:
    """Install the DB-backed :class:`SystemConfigLocaleProvider`.

    Called from :func:`bootstrap_production`. The binding is global
    (module-level state inside ``src.core.i18n``) because the i18n
    helper deliberately has no injected handle to keep its call sites
    simple — the composition root is the only place that flips it from
    the static default to a real DB-backed provider.
    """

    from src.application.system_config import SystemConfigLocaleProvider
    from src.core.i18n import set_locale_provider

    set_locale_provider(SystemConfigLocaleProvider())


def install_test_locale_provider(provider: Any = None) -> None:
    """Install a :class:`LocaleProvider` for tests / callers.

    Tests that need a deterministic default locale call this with a
    :class:`~src.application.system_config.StaticLocaleProvider` (or
    leave ``provider=None`` to install one that returns
    :data:`src.core.i18n.DEFAULT_LOCALE`). Also re-exported as a public
    helper so pytest fixtures can call it directly without invoking the
    full composition root.

    Type is written as ``Any`` to avoid importing the Protocol class at
    module scope — that keeps the composition root's import graph
    identical to the pre-Todo-8 shape.
    """

    from src.application.system_config import StaticLocaleProvider
    from src.core.i18n import set_locale_provider

    set_locale_provider(provider if provider is not None else StaticLocaleProvider())


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _verify_port(
    instance: Any,
    port: type,
    *,
    binding: str,
) -> None:
    """Defensive runtime check that ``instance`` satisfies ``port``.

    The ``Protocol`` classes are decorated with ``@runtime_checkable``
    so ``isinstance`` is meaningful. We use this purely as a guardrail
    against accidentally feeding an unbound mock into the composition
    root; static type checking still does the heavy lifting.
    """

    if not isinstance(instance, port):
        raise ConfigurationError(
            (
                f"Composition binding {binding!r} expected an instance of "
                f"{port.__name__}; got {type(instance).__name__}"
            ),
            binding=binding,
        )


__all__ = [
    "Container",
    "bootstrap_for_tests",
    "bootstrap_production",
    "install_test_locale_provider",
]
