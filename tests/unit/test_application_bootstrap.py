"""Unit tests for ``src.application.bootstrap`` (composition root).

These tests lock the public surface of the composition root that every
Wave 1 use case will eventually consume:

* ``bootstrap_for_tests()`` returns a ``Container`` populated with fakes
  by default, so unit tests can build hermetic use cases without
  monkeypatching anything under ``src.infrastructure``.
* Every binding is overridable through a keyword argument; overrides
  that do not satisfy the corresponding port raise
  :class:`ConfigurationError` with the offending binding name.
* ``bootstrap_production()`` constructs the real adapters but never
  opens a Redis / broker connection at import time — touching its
  methods only fails when a real broker round-trip is needed.

The tests are deliberately hermetic: no monkeypatching, no test
fixtures that touch Celery / Redis / SQLAlchemy. They prove that the
composition root alone is enough to build a usable container for
application code.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.application.bootstrap import (
    Container,
    bootstrap_for_tests,
    bootstrap_production,
)
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
# bootstrap_for_tests default behaviour
# ---------------------------------------------------------------------------


def test_bootstrap_for_tests_returns_container_with_all_fakes() -> None:
    """Default bindings come from ``bootstrap_fakes``; every port is populated."""

    container = bootstrap_for_tests()

    assert isinstance(container, Container)
    assert isinstance(container.dispatcher, FakeTaskDispatcher)
    assert isinstance(container.llm_factory, FakeLLMGatewayFactory)
    assert isinstance(container.clock, FakeClock)
    assert isinstance(container.id_gen, FakeIdGenerator)
    assert isinstance(container.task_control, FakeTaskControl)


def test_bootstrap_for_tests_ports_satisfy_protocols() -> None:
    """Every binding must be a runtime-checkable Protocol instance."""

    container = bootstrap_for_tests()

    assert isinstance(container.dispatcher, TaskDispatcherPort)
    assert isinstance(container.llm_factory, LLMGatewayFactoryPort)
    assert isinstance(container.clock, ClockPort)
    assert isinstance(container.id_gen, IdGeneratorPort)
    assert isinstance(container.task_control, TaskControlPort)


def test_bootstrap_for_tests_returns_fresh_containers_each_call() -> None:
    """Each call yields a brand-new container; no shared mutable state."""

    first = bootstrap_for_tests()
    second = bootstrap_for_tests()

    assert first is not second
    assert first.dispatcher is not second.dispatcher
    assert first.clock is not second.clock
    assert first.id_gen is not second.id_gen


# ---------------------------------------------------------------------------
# bootstrap_for_tests overrides
# ---------------------------------------------------------------------------


def test_bootstrap_for_tests_accepts_dispatcher_override() -> None:
    """Custom dispatcher flows through to the Container unchanged."""

    custom = FakeTaskDispatcher(task_id_prefix="custom")

    container = bootstrap_for_tests(dispatcher=custom)

    assert container.dispatcher is custom
    # Smoke: the custom prefix must survive a real call so the binding
    # is wired through, not copied.
    task_id = container.dispatcher.dispatch_webhook(
        # SendWebhookCommand lives in pipeline.commands but the fake
        # only inspects the argument object identity, so any object
        # works; using the dataclass keeps the test realistic.
        _build_webhook_command(),
    )
    assert task_id == "custom-1"
    assert custom.dispatched_webhook[-1].event_type == "test.event"


def test_bootstrap_for_tests_accepts_clock_override() -> None:
    """Custom clock flows through and is observable via ``now()``."""

    fixed = datetime(2030, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    custom = FakeClock(initial=fixed)

    container = bootstrap_for_tests(clock=custom)

    assert container.clock is custom
    assert container.clock.now() == fixed


def test_bootstrap_for_tests_accepts_id_gen_override() -> None:
    """Custom ID generator flows through and is observable via ``new_id()``."""

    custom = FakeIdGenerator(prefix="override")

    container = bootstrap_for_tests(id_gen=custom)

    assert container.id_gen is custom
    assert container.id_gen.new_id() == "override-1"
    assert container.id_gen.new_id() == "override-2"


def test_bootstrap_for_tests_accepts_task_control_override() -> None:
    """Custom task control flows through and records revoke / heartbeat."""

    custom = FakeTaskControl(default_worker_count=3, default_active_count=2)

    container = bootstrap_for_tests(task_control=custom)

    assert container.task_control is custom
    container.task_control.revoke("task-xyz", terminate=True)
    assert custom.revocations == [("task-xyz", True)]


def test_bootstrap_for_tests_accepts_llm_factory_override() -> None:
    """Custom LLM factory flows through and returns fake gateways on build()."""

    custom = FakeLLMGatewayFactory()

    container = bootstrap_for_tests(llm_factory=custom)

    assert container.llm_factory is custom
    gateway = container.llm_factory.build(
        api_base_url="http://localhost",
        api_key="key",
        model_name="m",
        timeout_seconds=30,
    )
    assert gateway.model_name == "m"
    assert custom.gateways[-1] == gateway


def test_bootstrap_for_tests_accepts_full_override() -> None:
    """All five bindings can be overridden simultaneously."""

    custom_dispatcher = FakeTaskDispatcher(task_id_prefix="all")
    custom_factory = FakeLLMGatewayFactory()
    custom_clock = FakeClock(initial=datetime(2030, 1, 1, tzinfo=timezone.utc))
    custom_id_gen = FakeIdGenerator(prefix="all")
    custom_task_control = FakeTaskControl()

    container = bootstrap_for_tests(
        dispatcher=custom_dispatcher,
        llm_factory=custom_factory,
        clock=custom_clock,
        id_gen=custom_id_gen,
        task_control=custom_task_control,
    )

    assert container.dispatcher is custom_dispatcher
    assert container.llm_factory is custom_factory
    assert container.clock is custom_clock
    assert container.id_gen is custom_id_gen
    assert container.task_control is custom_task_control


# ---------------------------------------------------------------------------
# bootstrap_for_tests validation
# ---------------------------------------------------------------------------


def test_bootstrap_for_tests_rejects_wrong_type_for_dispatcher() -> None:
    """Passing a non-dispatcher object raises ConfigurationError with binding name."""

    class NotADispatcher:
        pass

    with pytest.raises(ConfigurationError) as exc_info:
        bootstrap_for_tests(dispatcher=NotADispatcher())  # type: ignore[arg-type]

    assert exc_info.value.binding == "dispatcher"
    assert "TaskDispatcherPort" in str(exc_info.value)


def test_bootstrap_for_tests_rejects_wrong_type_for_clock() -> None:
    """Clock binding is also validated."""

    class NotAClock:
        pass

    with pytest.raises(ConfigurationError) as exc_info:
        bootstrap_for_tests(clock=NotAClock())  # type: ignore[arg-type]

    assert exc_info.value.binding == "clock"


def test_bootstrap_for_tests_rejects_wrong_type_for_id_gen() -> None:
    """ID generator binding is also validated."""

    class NotAnIdGen:
        pass

    with pytest.raises(ConfigurationError) as exc_info:
        bootstrap_for_tests(id_gen=NotAnIdGen())  # type: ignore[arg-type]

    assert exc_info.value.binding == "id_gen"


def test_bootstrap_for_tests_rejects_wrong_type_for_task_control() -> None:
    """Task control binding is also validated."""

    class NotATaskControl:
        pass

    with pytest.raises(ConfigurationError) as exc_info:
        bootstrap_for_tests(task_control=NotATaskControl())  # type: ignore[arg-type]

    assert exc_info.value.binding == "task_control"


def test_bootstrap_for_tests_rejects_wrong_type_for_llm_factory() -> None:
    """LLM factory binding is also validated."""

    class NotAFactory:
        pass

    with pytest.raises(ConfigurationError) as exc_info:
        bootstrap_for_tests(llm_factory=NotAFactory())  # type: ignore[arg-type]

    assert exc_info.value.binding == "llm_factory"


# ---------------------------------------------------------------------------
# Fakes provide deterministic behaviour
# ---------------------------------------------------------------------------


def test_fake_dispatcher_records_every_command() -> None:
    """The fake dispatcher logs each command type independently."""

    dispatcher = FakeTaskDispatcher()

    build_id = dispatcher.dispatch_session_build(_build_session_build_command(source_id=1))
    analyze_id = dispatcher.dispatch_analyze_session(_build_analyze_command(session_id=2))
    summary_id = dispatcher.dispatch_generate_daily_summary(
        _build_daily_summary_command(target_date_str="2025-01-01")
    )
    webhook_id = dispatcher.dispatch_webhook(_build_webhook_command())

    assert build_id == "fake-task-1"
    assert analyze_id == "fake-task-2"
    assert summary_id == "fake-task-3"
    assert webhook_id == "fake-task-4"
    assert dispatcher.total_dispatched == 4
    assert dispatcher.dispatched_session_build[0].source_id == 1
    assert dispatcher.dispatched_analyze_session[0].session_id == 2
    assert dispatcher.dispatched_daily_summary[0].target_date_str == "2025-01-01"


def test_fake_dispatcher_supports_scripted_returns() -> None:
    """``set_next_return`` lets tests simulate dedupe / supersession semantics."""

    dispatcher = FakeTaskDispatcher()

    dispatcher.set_next_return(None)
    assert dispatcher.dispatch_session_build(_build_session_build_command(source_id=1)) is None

    dispatcher.set_next_return("fixed-id")
    assert (
        dispatcher.dispatch_session_build(_build_session_build_command(source_id=2)) == "fixed-id"
    )
    # Scripted return is single-use — the next call falls back to the counter.
    assert (
        dispatcher.dispatch_session_build(_build_session_build_command(source_id=3))
        == "fake-task-1"
    )


def test_fake_clock_is_deterministic_and_advances() -> None:
    """The fake clock starts at the documented epoch and is tickable."""

    clock = FakeClock()

    assert clock.now() == FakeClock.epoch_utc
    clock.tick()
    assert clock.now() == FakeClock.epoch_utc + timedelta(seconds=1)
    clock.tick(by=timedelta(hours=2))
    assert clock.now() == FakeClock.epoch_utc + timedelta(hours=2, seconds=1)
    assert clock.ticks == [timedelta(seconds=1), timedelta(hours=2)]


def test_fake_clock_rejects_naive_utc_inputs() -> None:
    """Naive datetimes are rejected because UTC requires ``tzinfo``."""

    with pytest.raises(ValueError):
        FakeClock(initial=datetime(2025, 1, 1))
    with pytest.raises(ValueError):
        FakeClock().set_now(datetime(2025, 1, 1))


def test_fake_id_generator_is_deterministic_by_default() -> None:
    """Default mode produces monotonic, human-readable IDs."""

    generator = FakeIdGenerator()

    assert generator.new_id() == "fake-id-1"
    assert generator.new_id() == "fake-id-2"
    assert generator.issued == ["fake-id-1", "fake-id-2"]


def test_fake_task_control_records_revoke_and_heartbeat() -> None:
    """Revoke + heartbeat are recorded without touching Celery."""

    control = FakeTaskControl(default_worker_count=4, default_active_count=1)

    control.revoke("a", terminate=False)
    control.revoke("b", terminate=True)
    report = control.heartbeat(queue="celery")

    assert control.revocations == [("a", False), ("b", True)]
    assert control.heartbeats == ["celery"]
    assert isinstance(report, HeartbeatReport)
    assert report.queue == "celery"
    assert report.worker_count == 4
    assert report.active_count == 1


def test_fake_llm_factory_returns_independent_gateways() -> None:
    """Each ``build()`` call returns a fresh gateway; closures do not leak."""

    factory = FakeLLMGatewayFactory()

    gateway_one = factory.build(
        api_base_url="http://a",
        api_key="k1",
        model_name="m1",
        timeout_seconds=10,
    )
    gateway_two = factory.build(
        api_base_url="http://b",
        api_key="k2",
        model_name="m2",
        timeout_seconds=20,
    )

    assert gateway_one is not gateway_two
    assert gateway_one.model_name == "m1"
    assert gateway_two.model_name == "m2"
    assert factory.build_calls[0]["api_key"] == "k1"
    assert factory.build_calls[1]["api_key"] == "k2"


def test_fake_llm_gateway_records_calls_and_returns_none_by_default() -> None:
    """Default behaviour: ``None`` replies, but calls are recorded."""

    gateway = FakeLLMGatewayFactory().build(
        api_base_url="http://x",
        api_key="k",
        model_name="m",
        timeout_seconds=10,
    )

    text = gateway.chat_completion(messages=[{"role": "user", "content": "hi"}])
    usage = gateway.get_last_usage()

    assert text is None
    assert usage is None
    assert len(gateway.completion_calls) == 1
    assert gateway.completion_calls[0]["messages"][0]["content"] == "hi"


# ---------------------------------------------------------------------------
# bootstrap_production shape (no broker round-trip)
# ---------------------------------------------------------------------------


def test_bootstrap_production_returns_container_with_real_adapters() -> None:
    """Production binding wires the Celery / OpenAI adapters behind the ports."""

    container = bootstrap_production()

    # Concrete classes live under src.infrastructure.* — the composition
    # root is the only place allowed to import them.
    from src.infrastructure.llm.openai_gateway import OpenAICompatGatewayFactory
    from src.infrastructure.tasks.celery_dispatcher import CeleryTaskDispatcher

    assert isinstance(container.dispatcher, CeleryTaskDispatcher)
    assert isinstance(container.llm_factory, OpenAICompatGatewayFactory)
    # System clock and UUID adapter are private; only verify protocol
    # membership so we don't expose internals via the test.
    assert isinstance(container.clock, ClockPort)
    assert isinstance(container.id_gen, IdGeneratorPort)
    assert isinstance(container.task_control, TaskControlPort)


def test_bootstrap_production_clock_returns_aware_utc() -> None:
    """The production clock must always produce timezone-aware UTC."""

    container = bootstrap_production()

    instant = container.clock.now()
    assert isinstance(instant, datetime)
    assert instant.tzinfo is not None
    assert instant.utcoffset() == timedelta(0)


def test_bootstrap_production_id_generator_returns_string_uuid() -> None:
    """Production ID generator returns string UUIDs of the documented shape."""

    container = bootstrap_production()

    new_id = container.id_gen.new_id()
    assert isinstance(new_id, str)
    # uuid4 hex length is 32 chars plus 4 hyphens = 36 chars total.
    assert len(new_id) == 36
    assert new_id.count("-") == 4


def test_bootstrap_production_does_not_require_redis_to_construct() -> None:
    """Constructing the production container does not touch the broker."""

    # If this raises, we have accidentally opened a Redis connection at
    # container construction time. We deliberately avoid calling
    # ``dispatch_*`` or ``control.revoke`` here because those need a
    # real broker; the construction itself must be safe.
    container = bootstrap_production()

    # The container is fresh on every call (no caching by design).
    assert container is not bootstrap_production()


# ---------------------------------------------------------------------------
# Locale provider binding (Todo 8)
# ---------------------------------------------------------------------------


def test_bootstrap_production_installs_db_backed_locale_provider() -> None:
    """``bootstrap_production`` wires ``SystemConfigLocaleProvider`` into core.i18n."""

    from src.application.system_config import SystemConfigLocaleProvider
    from src.core import i18n

    saved = i18n.get_locale_provider()
    try:
        bootstrap_production()
        assert isinstance(i18n.get_locale_provider(), SystemConfigLocaleProvider)
    finally:
        i18n.set_locale_provider(saved)


def test_bootstrap_for_tests_installs_static_locale_provider() -> None:
    """``bootstrap_for_tests`` swaps the i18n provider for an in-memory default."""

    from src.application.system_config import StaticLocaleProvider
    from src.core import i18n

    saved = i18n.get_locale_provider()
    try:
        bootstrap_for_tests()
        assert isinstance(i18n.get_locale_provider(), StaticLocaleProvider)
        # The installed provider must still expose the default locale.
        assert i18n.get_system_default_locale() == "zh-CN"
    finally:
        i18n.set_locale_provider(saved)


# ---------------------------------------------------------------------------
# Helpers — command builders
# ---------------------------------------------------------------------------


def _build_session_build_command(source_id: int):
    from src.application.pipeline.commands import SessionBuildCommand

    return SessionBuildCommand(source_id=source_id, scan_mode="hot")


def _build_analyze_command(session_id: int):
    from src.application.pipeline.commands import AnalyzeSessionCommand

    return AnalyzeSessionCommand(session_id=session_id, priority="hot")


def _build_daily_summary_command(target_date_str: str | None):
    from src.application.pipeline.commands import GenerateDailySummaryCommand

    return GenerateDailySummaryCommand(target_date_str=target_date_str)


def _build_webhook_command():
    from src.application.pipeline.commands import SendWebhookCommand

    return SendWebhookCommand(event_type="test.event", payload={"k": "v"})
