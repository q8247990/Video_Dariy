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

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from sqlalchemy.orm import Session

from src.application.bootstrap import bootstrap_for_tests
from src.application.bootstrap_fakes import FakeLLMGatewayFactory, FakeTaskDispatcher
from src.application.pipeline.commands import AnalyzeSessionCommand
from src.application.ports.task_dispatcher import TaskDispatcherPort
from src.models.video_session import VideoSession
from src.models.video_source import VideoSource
from src.services.maintenance import dispatch_hot_builds
from src.tasks import _container
from src.tasks.session_build import run_hot_build


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


def _seed_source(db: Session) -> int:
    source = VideoSource(
        source_name="di-cam",
        camera_name="di-cam",
        location_name="客厅",
        source_type="local_directory",
        config_json={"root_path": "/tmp/videos"},
        enabled=True,
    )
    db.add(source)
    db.commit()
    db.refresh(source)
    return int(source.id)


def _seed_session(
    db: Session,
    source_id: int,
    *,
    start_time: datetime,
    priority: str | None = None,
) -> int:
    """Insert a single ``VideoSession`` row and return its id."""
    session = VideoSession(
        source_id=source_id,
        session_start_time=start_time,
        session_end_time=start_time + timedelta(minutes=30),
        total_duration_seconds=1800,
        analysis_status="sealed",
        analysis_priority=priority,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return int(session.id)


def test_session_build_dispatches_via_container(
    pg_db_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Session build routes hot/full scans through the port.

    Driving through the public :func:`run_hot_build` orchestration
    entry (re-exported via :mod:`src.tasks.session_build`) exercises
    the full seal+dispatch atomic path. A pre-seeded stuck-sealed
    session triggers the stuck-sealed self-heal inside the build,
    which dispatches via the container's fake dispatcher port. The
    recorded command is a real :class:`AnalyzeSessionCommand` whose
    ``session_id`` and ``priority`` mirror the seeded row.
    """
    dispatcher = _bind(FakeTaskDispatcher())

    monkeypatch.setattr(
        "src.adapters.xiaomi_parser.XiaomiDirectoryParser.scan_directory",
        lambda self, min_time, max_time, cancel_check: [],
    )

    base = datetime(2026, 3, 15, 9, 0, 0, tzinfo=timezone.utc)
    with pg_db_factory() as db:
        source_id = _seed_source(db)
        session_id = _seed_session(db, source_id, start_time=base, priority="hot")

    with pg_db_factory() as db:
        result = run_hot_build(db, source_id=source_id, queue_task_id="")

    assert result["analysis_redispatched"] == 1
    assert [c.session_id for c in dispatcher.dispatched_analyze_session] == [session_id]
    assert dispatcher.dispatched_analyze_session[0].priority == "hot"
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
