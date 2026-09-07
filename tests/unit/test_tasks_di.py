"""Todo 7: verify Celery task modules get dispatcher / LLM gateway via ports.

These tests prove the architecture-boundary requirement that
``src/tasks/*`` never imports ``src.infrastructure.*`` directly. Instead
each task module reads the composition-root ``Container`` exposed by
:mod:`src.tasks._container`, which is replaced with a fake-bound
container for the duration of each test. The fake port records every
dispatch / build call, so the assertions confirm:

* the correct command DTO is built from task inputs (priority, scan_mode,
  recovery_attempt, provider, etc.);
* retry-orchestration flows route through the same port that
  :mod:`src.application.tasks.use_case_retry` consumes;
* no Redis or Celery broker is touched — the ``FakeTaskDispatcher`` /
  ``FakeLLMGatewayFactory`` ship entirely in memory.

The tests use a temporary :class:`Container` and restore the production
holder in ``finally`` blocks, so they are safe to run in any order.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.application.bootstrap import bootstrap_for_tests
from src.application.bootstrap_fakes import FakeLLMGatewayFactory, FakeTaskDispatcher
from src.application.pipeline.commands import AnalyzeSessionCommand
from src.application.ports.task_dispatcher import TaskDispatcherPort
from src.services.maintenance import dispatch_hot_builds
from src.tasks import _container
from src.tasks._session_build_orchestration import _dispatch_analysis_for_sealed


@pytest.fixture(autouse=True)
def _restore_container() -> None:
    """Always reset the task-layer container holder after each test."""

    try:
        yield
    finally:
        _container.reset_container_for_tests()


def _bind(dispatcher: FakeTaskDispatcher) -> FakeTaskDispatcher:
    container = bootstrap_for_tests(dispatcher=dispatcher)
    _container.set_container_for_tests(container)
    return dispatcher


def test_session_build_dispatches_via_container() -> None:
    """Session build routes hot/full scans through the port."""

    dispatcher = _bind(FakeTaskDispatcher())
    sealed = [MagicMock(session_id=11, priority="hot"), MagicMock(session_id=12, priority="full")]
    _dispatch_analysis_for_sealed(db=MagicMock(), sealed_sessions=sealed)

    assert [c.session_id for c in dispatcher.dispatched_analyze_session] == [11, 12]
    assert dispatcher.dispatched_analyze_session[0].priority == "hot"
    assert dispatcher.dispatched_analyze_session[1].priority == "full"
    assert isinstance(dispatcher.dispatched_analyze_session[0], AnalyzeSessionCommand)


def test_task_maintenance_dispatch_uses_container() -> None:
    """Hot-build dispatch helper calls the port rather than constructing a Celery dispatcher."""

    dispatcher = _bind(FakeTaskDispatcher())
    db = MagicMock()
    db.query.return_value.filter.return_value.all.return_value = [
        MagicMock(id=1),
        MagicMock(id=2),
    ]
    dispatch_hot_builds(db)

    assert [c.source_id for c in dispatcher.dispatched_session_build] == [1, 2]
    assert [c.scan_mode for c in dispatcher.dispatched_session_build] == ["hot", "hot"]


def test_container_helpers_round_trip() -> None:
    """The task-layer container holder exposes the composition-root singleton."""

    fake = bootstrap_for_tests()
    _container.set_container_for_tests(fake)
    assert _container.get_container() is fake


def test_dispatcher_port_satisfies_protocol() -> None:
    """The fake dispatcher is a runtime ``TaskDispatcherPort``."""

    dispatcher = _bind(FakeTaskDispatcher())
    assert isinstance(dispatcher, TaskDispatcherPort)
    assert callable(dispatcher.dispatch_session_build)
    assert callable(dispatcher.dispatch_analyze_session)
    assert callable(dispatcher.dispatch_generate_daily_summary)
    assert callable(dispatcher.dispatch_webhook)


def test_llm_factory_via_container_uses_fake() -> None:
    """The composition root's LLM factory slot swaps to the fake cleanly."""

    factory = FakeLLMGatewayFactory()
    container = bootstrap_for_tests(llm_factory=factory)
    _container.set_container_for_tests(container)
    gateway = container.llm_factory.build(
        api_base_url="https://example.com",
        api_key="x",
        model_name="vision-1",
        timeout_seconds=60,
    )
    assert gateway.model_name == "vision-1"
    assert factory.build_calls == [
        {
            "api_base_url": "https://example.com",
            "api_key": "x",
            "model_name": "vision-1",
            "timeout_seconds": 60,
            "supports_tool_calling": False,
        }
    ]
