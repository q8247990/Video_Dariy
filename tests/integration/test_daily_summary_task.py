"""Daily-summary Celery tasks — public-entry integration tests (Todo "测试边界收敛" wave 2).

Wave 1 rewrote the analyzer-cluster tests to drive the public Celery
task entry :func:`src.tasks.analyzer.analyze_session_task.run` through
the :class:`src.tasks._container.Container` holder, with the LLM
gateway replaced by an in-memory
:class:`~src.application.bootstrap_fakes.ScriptedVisionGateway` and
the analyzer helpers by an
:class:`~src.application.bootstrap_fakes.FakeAnalysisPorts` bundle
— the "group + base-action" pattern. This file ports the same
pattern to the summarizer cluster:

* every test seeds its own DB rows via :func:`pg_db_factory`,
  configures the scripted gateway's responses on the container's
  ``llm_factory`` and calls the public Celery task entry
  (:func:`summarizer.generate_daily_summary_task.run` /
  :func:`summarizer.dispatch_scheduled_daily_summary_task.run`) or
  the public orchestration entry
  :func:`summarizer._summarizer_orchestration.run_dispatch_scheduled`
  / :func:`summarizer._summarizer_orchestration.run_daily_summary_generation`
  when the Celery wrapper does not expose the required parameter
  (e.g. deterministic ``now``, threshold override);

* dispatch behaviour is asserted through the
  :class:`~src.application.bootstrap_fakes.FakeTaskDispatcher`
  records attached to the container — the container seam is the
  :func:`src.tasks._summarizer_orchestration._get_pipeline_orchestrator`
  helper's only consumer of the ``dispatcher`` port, so replacing the
  port's binding eliminates the need to monkey-patch the internal
  helper;

* single-pass vs serial is proven by counting
  :meth:`~src.application.bootstrap_fakes.ScriptedVisionGateway.chat_completion`
  calls and asserting the final ``DailySummary`` row — neither the
  private :data:`~src.services.summarizer.constants.SERIAL_SPLIT_PROMPT_THRESHOLD`
  constant nor the internal ``detail_json["summary_mode"]`` tag
  (which is written but never read by any production caller) are
  reached for.

The only allowed monkey-patches against ``src`` internals are:

* :func:`src.db.session.SessionLocal` rebound to the
  ``postgres_real_engine``-bound sessionmaker (DB-env seam);
* :func:`src.application.outbox.contracts._reset_emitted_event_ids_for_testing`
  reset (authorised ``*_for_testing`` hook).
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterator
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

import src.db.session as db_session_module
from src.application.bootstrap import bootstrap_for_tests
from src.application.bootstrap_fakes import (
    FakeTaskDispatcher,
    ScriptedVisionGateway,
    ScriptedVisionGatewayFactory,
)
from src.application.outbox.contracts import OutboxStatus, _reset_emitted_event_ids_for_testing
from src.models.daily_summary import DailySummary
from src.models.event_record import EventRecord
from src.models.home_entity_profile import HomeEntityProfile
from src.models.llm_provider import LLMProvider
from src.models.outbox import OutboxEvent
from src.models.system_config import SystemConfig
from src.models.task_log import TaskLog
from src.models.video_session import VideoSession
from src.models.video_source import VideoSource
from src.models.webhook_config import WebhookConfig
from src.services.home_timezone import local_day_bounds
from src.tasks import _container, summarizer
from src.tasks._summarizer_orchestration import (
    run_daily_summary_generation,
    run_dispatch_scheduled,
)

SessionFactory = Callable[[], Session]


# ---------------------------------------------------------------------------
# Base action — module-scoped autouse fixture that prepares the task-layer
# container holder. Mirrors ``tests/unit/test_analyzer_task.py``.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _base_action(
    postgres_real_engine: Engine,
    pg_db_factory: SessionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    """Wire the task-layer container holder + DB seam for every test.

    The standard :class:`FakeTaskDispatcher` records every dispatch
    attempt and returns deterministic ``"fake-task-N"`` ids. The
    standard :class:`ScriptedVisionGatewayFactory` returns a fresh
    :class:`ScriptedVisionGateway` per :meth:`build` call; tests that
    need scripted text responses either :func:`queue_responses` on the
    installed gateway (single-pass path) or substitute a local
    branching factory (serial path).

    ``db_session_module.SessionLocal`` is monkey-patched to a
    sessionmaker bound to ``postgres_real_engine`` so the
    ``task_db_session`` context manager opens sessions against the
    same engine :func:`pg_db_factory` uses; commits the worker makes
    are visible from the test's verify session, and the per-test
    ``TRUNCATE … RESTART IDENTITY CASCADE`` keeps test isolation.

    :func:`outbox_contracts._reset_emitted_event_ids_for_testing` is
    called in setup and teardown so the outbox contract layer's
    in-memory emitted-id set does not leak across tests.
    """

    dispatcher = FakeTaskDispatcher()
    factory = ScriptedVisionGatewayFactory()
    container = bootstrap_for_tests(dispatcher=dispatcher, llm_factory=factory)
    _container.set_container_for_tests(container)

    local_session = sessionmaker(bind=postgres_real_engine, autocommit=False, autoflush=False)
    monkeypatch.setattr(db_session_module, "SessionLocal", local_session)
    _reset_emitted_event_ids_for_testing()

    yield

    _reset_emitted_event_ids_for_testing()
    _container.reset_container_for_tests()


def _seed_qa_provider(db: Session) -> int:
    """Seed the enabled default-QA :class:`LLMProvider`.

    The scripted gateway bypasses decryption, so an empty ``api_key``
    is fine. ``is_default_qa`` + ``supports_qa`` + ``enabled`` are the
    only fields :func:`find_required_enabled_provider` checks.
    """
    provider = LLMProvider(
        provider_name="qa-default",
        provider_type="qa_provider",
        api_base_url="https://example.com/v1",
        api_key="dummy-key",
        model_name="gpt-4o-mini",
        timeout_seconds=30,
        retry_count=1,
        enabled=True,
        supports_qa=True,
        is_default_qa=True,
        supports_vision=False,
        is_default_vision=False,
    )
    db.add(provider)
    db.flush()
    return provider.id


def _create_source_and_session(
    db: Session,
    *,
    session_start_time: datetime,
    session_end_time: datetime,
) -> tuple[int, int]:
    """Insert a ``VideoSource`` + ``VideoSession`` pair and return their IDs."""
    source = VideoSource(
        source_name="test-source",
        camera_name="test-camera",
        location_name="home",
        source_type="ipcamera",
        enabled=True,
    )
    db.add(source)
    db.flush()
    session = VideoSession(
        source_id=source.id,
        session_start_time=session_start_time,
        session_end_time=session_end_time,
    )
    db.add(session)
    db.flush()
    return int(source.id), int(session.id)


def _latest_gateway() -> ScriptedVisionGateway:
    """Return the gateway the latest run consumed (base action rebuilds per test)."""

    factory = _container.get_container().llm_factory
    assert isinstance(factory, ScriptedVisionGatewayFactory)
    return factory.gateways[-1]


def _set_text_responses(*responses: str) -> None:
    """Queue scripted text responses on the next gateway to be built.

    The summarizer pipeline opens exactly one gateway per
    :func:`summarizer.generate_daily_summary_task.run` call, so
    queuing responses here is enough for a single test run. Tests
    that drive the orchestrator twice either call this helper twice
    or replace the factory's installed gateway between runs.
    """

    factory = _container.get_container().llm_factory
    assert isinstance(factory, ScriptedVisionGatewayFactory)
    if factory._gateway is not None:
        factory._gateway.queue_responses(list(responses))
    else:
        factory.install_gateway(ScriptedVisionGateway(responses=list(responses)))


# ---------------------------------------------------------------------------
# Local fake gateway — branching responses (serial path)
# ---------------------------------------------------------------------------


class _BranchingTextGateway(ScriptedVisionGateway):
    """Text-mode LLM gateway that picks responses per call based on prompt content.

    The summarizer's serial path issues one ``chat_completion`` per
    subject plus one rollup call; the subject prompt template contains
    the marker ``"对象摘要任务"`` while the rollup prompt does not.
    The default scripted gateway only pops from a FIFO queue, which
    cannot distinguish the two call shapes, so the serial-path test
    installs this branching fake instead. The base class supplies all
    other protocol methods (``get_last_usage`` / ``close`` / …).
    """

    def __init__(
        self,
        *,
        subject_response: str,
        rollup_response: str,
    ) -> None:
        super().__init__(responses=[])
        self._subject_response = subject_response
        self._rollup_response = rollup_response

    def chat_completion(
        self,
        messages: list[dict[str, Any]],
        temperature: float = 0.2,
        max_tokens: int | None = None,
        response_format: dict[str, Any] | None = None,
        extra_body: dict[str, Any] | None = None,
    ) -> str | None:
        snapshot = {
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "response_format": response_format,
            "extra_body": extra_body,
        }
        prompt = str(messages[-1]["content"])
        if "对象摘要任务" in prompt:
            response = self._subject_response
        else:
            response = self._rollup_response
        self.calls.append({"response": response, **snapshot})
        return response


class _BranchingTextGatewayFactory(ScriptedVisionGatewayFactory):
    """Factory that hands out a single shared :class:`_BranchingTextGateway`."""

    def __init__(self, gateway: _BranchingTextGateway) -> None:
        super().__init__(gateway=gateway)


def _install_branching_gateway(
    *,
    subject_response: str,
    rollup_response: str,
) -> _BranchingTextGateway:
    """Replace the container's LLM factory with one returning a branching fake.

    Used by the serial-path test. The factory's :meth:`build` returns
    the same gateway for every call, so all subject calls get the
    subject-style reply and the rollup call gets the rollup-style
    reply.
    """

    gateway = _BranchingTextGateway(
        subject_response=subject_response,
        rollup_response=rollup_response,
    )
    factory = _BranchingTextGatewayFactory(gateway)
    container = bootstrap_for_tests(
        dispatcher=_container.get_container().dispatcher,
        llm_factory=factory,
    )
    _container.set_container_for_tests(container)
    return gateway


# ---------------------------------------------------------------------------
# Happy path — empty / structured / timezone / cancel  (A tests preserved)
# ---------------------------------------------------------------------------


def test_generate_daily_summary_empty_events_fallback_and_no_webhook(
    pg_db_factory: SessionFactory,
) -> None:
    """No events on the day → no LLM call returns a usable payload → fallback.

    The gateway returns ``""`` (empty), the single-pass parser branch
    skips and the orchestrator substitutes the stable empty-day
    :func:`~src.core.i18n.locale_directive.get_fallback_summary` text;
    no :class:`WebhookConfig` rows match the
    ``daily_summary_generated`` subscription so the outbox writer
    enrols zero webhook rows.
    """

    db = pg_db_factory()
    try:
        _seed_qa_provider(db)
        db.commit()
    finally:
        db.close()

    _set_text_responses("")

    result = summarizer.generate_daily_summary_task.run("2026-03-13")

    assert result["summary_date"] == "2026-03-13"
    assert result["event_count"] == 0
    assert len(_latest_gateway().calls) == 1

    verify_db = pg_db_factory()
    try:
        summary = (
            verify_db.query(DailySummary)
            .filter(DailySummary.summary_date == datetime(2026, 3, 13).date())
            .first()
        )
        assert summary is not None
        assert summary.overall_summary == "昨天家中整体较为平稳，未观测到明确的关键活动。"
        assert summary.subject_sections_json == []
        assert summary.attention_items_json == []
        assert verify_db.query(OutboxEvent).count() == 0
        dispatcher = _container.get_container().dispatcher
        assert isinstance(dispatcher, FakeTaskDispatcher)
        assert dispatcher.dispatched_webhook == []
    finally:
        verify_db.close()


def test_generate_daily_summary_structured_persist_success(
    pg_db_factory: SessionFactory,
) -> None:
    """Two seeded events → structured response payload persists verbatim."""

    db = pg_db_factory()
    try:
        _seed_qa_provider(db)
        source_id, session_id = _create_source_and_session(
            db,
            session_start_time=datetime(2026, 3, 13, 0, 0, 0, tzinfo=timezone.utc),
            session_end_time=datetime(2026, 3, 13, 23, 59, 59, tzinfo=timezone.utc),
        )

        db.add_all(
            [
                HomeEntityProfile(
                    entity_type="member",
                    name="爸爸",
                    role_type="father",
                    age_group="adult",
                    is_enabled=True,
                    sort_order=0,
                ),
                HomeEntityProfile(
                    entity_type="pet",
                    name="布丁",
                    role_type="cat",
                    is_enabled=True,
                    sort_order=1,
                ),
                EventRecord(
                    source_id=source_id,
                    session_id=session_id,
                    event_start_time=datetime(2026, 3, 13, 9, 0, 0, tzinfo=timezone.utc),
                    description="成员出现在客厅",
                    event_type="member_appear",
                    title="成员出现",
                    summary="爸爸上午出现在客厅并活动",
                    related_entities_json=[
                        {
                            "entity_type": "member",
                            "display_name": "爸爸",
                            "matched_profile_name": "爸爸",
                            "recognition_status": "confirmed",
                        }
                    ],
                ),
                EventRecord(
                    source_id=source_id,
                    session_id=session_id,
                    event_start_time=datetime(2026, 3, 13, 10, 0, 0, tzinfo=timezone.utc),
                    description="门口出现未知人员",
                    event_type="unknown_person_appear",
                    title="未知人员出现",
                    summary="门口出现未知人员短暂停留",
                ),
            ]
        )
        db.commit()
    finally:
        db.close()

    response_payload = {
        "overall_summary": "昨天家中整体平稳，爸爸在客厅有活动，门口有一次未知人员短暂停留。",
        "subject_sections": [
            {
                "subject_name": "爸爸",
                "subject_type": "member",
                "summary": "爸爸上午在客厅有明确活动。",
                "attention_needed": False,
            }
        ],
        "attention_items": [
            {
                "title": "门口未知人员",
                "summary": "门口有一次未知人员短暂停留，建议关注。",
                "level": "medium",
            }
        ],
    }
    _set_text_responses(json.dumps(response_payload, ensure_ascii=False))

    result = summarizer.generate_daily_summary_task.run("2026-03-13")

    assert result["summary_date"] == "2026-03-13"
    assert result["event_count"] == 2
    assert len(_latest_gateway().calls) == 1

    verify_db = pg_db_factory()
    try:
        summary = (
            verify_db.query(DailySummary)
            .filter(DailySummary.summary_date == datetime(2026, 3, 13).date())
            .first()
        )
        assert summary is not None
        assert summary.summary_title == "2026-03-13 家庭日报"
        assert summary.overall_summary.startswith("昨天家中整体平稳")
        subject_sections = summary.subject_sections_json or []
        assert len(subject_sections) == 2
        assert subject_sections[0]["subject_name"] == "爸爸"
        assert subject_sections[0]["activity_score"] == 1
        assert subject_sections[1]["subject_name"] == "布丁"
        assert subject_sections[1]["activity_score"] == 0
        assert len(summary.attention_items_json or []) == 1
    finally:
        verify_db.close()


def test_generate_daily_summary_uses_home_timezone_half_open_event_range(
    pg_db_factory: SessionFactory,
) -> None:
    """Asia/Shanghai local day maps to a half-open UTC range that includes 2 of 4 events."""

    db = pg_db_factory()
    try:
        _seed_qa_provider(db)
        source_id, session_id = _create_source_and_session(
            db,
            session_start_time=datetime(2026, 3, 12, 0, 0, 0, tzinfo=timezone.utc),
            session_end_time=datetime(2026, 3, 14, 0, 0, 0, tzinfo=timezone.utc),
        )
        db.add(SystemConfig(config_key="home_timezone", config_value="Asia/Shanghai"))
        db.add_all(
            [
                EventRecord(
                    source_id=source_id,
                    session_id=session_id,
                    event_start_time=datetime(2026, 3, 12, 16, tzinfo=timezone.utc),
                    description="local day start",
                ),
                EventRecord(
                    source_id=source_id,
                    session_id=session_id,
                    event_start_time=datetime(2026, 3, 13, 15, 59, 59, tzinfo=timezone.utc),
                    description="local day end",
                ),
                EventRecord(
                    source_id=source_id,
                    session_id=session_id,
                    event_start_time=datetime(2026, 3, 12, 15, 59, 59, tzinfo=timezone.utc),
                    description="previous local day",
                ),
                EventRecord(
                    source_id=source_id,
                    session_id=session_id,
                    event_start_time=datetime(2026, 3, 13, 16, tzinfo=timezone.utc),
                    description="next local day",
                ),
            ]
        )
        db.commit()
    finally:
        db.close()

    _set_text_responses('{"overall_summary":"stable","subject_sections":[],"attention_items":[]}')

    result = summarizer.generate_daily_summary_task.run("2026-03-13")

    assert result["event_count"] == 2
    assert len(_latest_gateway().calls) == 1


@pytest.mark.postgres
def test_postgres_event_query_maps_new_york_local_day_to_utc_half_open_range(
    pg_db: Session,
) -> None:
    source = VideoSource(
        source_name="new-york-camera",
        camera_name="living",
        location_name="home",
        source_type="ipcamera",
    )
    pg_db.add(source)
    pg_db.flush()
    session = VideoSession(
        source_id=source.id,
        session_start_time=datetime(2026, 3, 8, 5, tzinfo=timezone.utc),
        session_end_time=datetime(2026, 3, 9, 4, tzinfo=timezone.utc),
    )
    pg_db.add(session)
    pg_db.flush()
    pg_db.add_all(
        [
            EventRecord(
                source_id=source.id,
                session_id=session.id,
                event_start_time=datetime(2026, 3, 8, 5, tzinfo=timezone.utc),
                description="local start",
            ),
            EventRecord(
                source_id=source.id,
                session_id=session.id,
                event_start_time=datetime(2026, 3, 9, 4, tzinfo=timezone.utc),
                description="next local start",
            ),
        ]
    )
    pg_db.flush()
    start, end = local_day_bounds(ZoneInfo("America/New_York"), datetime(2026, 3, 8).date())

    events = (
        pg_db.query(EventRecord)
        .filter(EventRecord.event_start_time >= start, EventRecord.event_start_time < end)
        .all()
    )

    assert [event.description for event in events] == ["local start"]


def test_generate_daily_summary_honors_cancel_requested(
    pg_db_factory: SessionFactory,
) -> None:
    """Pre-existing TaskLog with ``cancel_requested=True`` short-circuits the run."""

    db = pg_db_factory()
    try:
        _seed_qa_provider(db)
        db.add(
            TaskLog(
                task_type="daily_summary_generation",
                task_target_id=None,
                queue_task_id="cancel-summary-task",
                status="running",
                cancel_requested=True,
                detail_json={
                    "target_date": "2026-03-13",
                    "dedupe_key": "daily_summary_generation|2026-03-13",
                },
            )
        )
        db.commit()
    finally:
        db.close()

    summarizer.generate_daily_summary_task.push_request(id="cancel-summary-task", retries=0)
    try:
        result = summarizer.generate_daily_summary_task.run("2026-03-13")
    finally:
        summarizer.generate_daily_summary_task.pop_request()

    assert result["cancelled"] is True
    assert _container.get_container().llm_factory.gateways == []

    verify_db = pg_db_factory()
    try:
        task_log = verify_db.query(TaskLog).filter_by(queue_task_id="cancel-summary-task").one()
        summary = (
            verify_db.query(DailySummary)
            .filter(DailySummary.summary_date == datetime(2026, 3, 13).date())
            .first()
        )

        assert task_log.status == "cancelled"
        assert task_log.cancel_requested is True
        assert summary is None
    finally:
        verify_db.close()


# ---------------------------------------------------------------------------
# Dispatch — container-seam rewrite (B tests)
# ---------------------------------------------------------------------------


def _seed_schedule(
    db: Session,
    *,
    schedule_text: str,
    zone_name: str,
) -> None:
    """Seed the per-source dispatch schedule and home timezone into SystemConfig."""

    db.add_all(
        [
            SystemConfig(config_key="daily_summary_schedule", config_value=schedule_text),
            SystemConfig(config_key="home_timezone", config_value=zone_name),
        ]
    )
    db.commit()


def test_dispatch_daily_summary_runs_once_per_target_date(
    pg_db_factory: SessionFactory,
) -> None:
    """Two consecutive dispatches: the first claims the per-date guard, the second is blocked."""

    db = pg_db_factory()
    try:
        # Schedule is seeded at the current wall-clock HH:MM so the
        # dispatcher's "is now past schedule" check passes without
        # having to drive ``now`` deterministically. The per-test
        # ``pg_db_factory`` TRUNCATE keeps the AppRuntimeState from
        # leaking the guard row between tests.
        now = datetime.now(timezone.utc)
        _seed_schedule(db, schedule_text=now.strftime("%H:%M"), zone_name="UTC")
    finally:
        db.close()

    first = summarizer.dispatch_scheduled_daily_summary_task.run()
    second = summarizer.dispatch_scheduled_daily_summary_task.run()

    assert first["scheduled"] is True
    assert second["scheduled"] is False
    assert second["reason"] == "dispatch_guard_blocked"

    dispatcher = _container.get_container().dispatcher
    assert isinstance(dispatcher, FakeTaskDispatcher)
    assert len(dispatcher.dispatched_daily_summary) == 1
    assert dispatcher.dispatched_daily_summary[0].target_date_str == first["target_date"]


def test_dispatch_daily_summary_retries_after_dispatch_failure(
    pg_db_factory: SessionFactory,
) -> None:
    """Dispatcher raises on the first attempt; the per-date guard releases; retry succeeds.

    The scripted dispatcher failure is implemented as a thin
    :class:`FakeTaskDispatcher` subclass — the test's own fake of a
    port — which raises :class:`RuntimeError` on the first call only.
    The orchestrator's catch-all releases the ``AppRuntimeState`` row
    so the second call can claim the guard again.
    """

    class _FirstCallFailsDispatcher(FakeTaskDispatcher):
        """Local fake dispatcher; raises ``RuntimeError`` on the first call only."""

        def __init__(self) -> None:
            super().__init__()
            self._failed = False

        def dispatch_generate_daily_summary(self, db: Any, command: Any) -> str | None:
            if not self._failed:
                self._failed = True
                raise RuntimeError("queue unavailable")
            return super().dispatch_generate_daily_summary(db, command)

    flaker = _FirstCallFailsDispatcher()
    container = bootstrap_for_tests(
        dispatcher=flaker,
        llm_factory=_container.get_container().llm_factory,
    )
    _container.set_container_for_tests(container)

    db = pg_db_factory()
    try:
        now = datetime.now(timezone.utc)
        _seed_schedule(db, schedule_text=now.strftime("%H:%M"), zone_name="UTC")
    finally:
        db.close()

    first = summarizer.dispatch_scheduled_daily_summary_task.run()
    second = summarizer.dispatch_scheduled_daily_summary_task.run()

    assert first["scheduled"] is False
    assert first["reason"] == "dispatch_failed"
    assert second["scheduled"] is True
    assert flaker._failed is True
    assert len(flaker.dispatched_daily_summary) == 1
    assert flaker.dispatched_daily_summary[0].target_date_str == second["target_date"]


def test_dispatch_daily_summary_uses_home_local_schedule_and_date(
    pg_db_factory: SessionFactory,
) -> None:
    """Asia/Shanghai 00:30 schedule + 00:31 local "now" dispatches the previous local date.

    The Celery task wrapper hard-codes ``home_now(zone)`` so the
    deterministic "now" has to be driven through the public
    orchestration entry :func:`run_dispatch_scheduled`, which exposes
    an explicit ``now`` keyword.
    """

    db = pg_db_factory()
    try:
        _seed_schedule(db, schedule_text="00:30", zone_name="Asia/Shanghai")
    finally:
        db.close()

    now = datetime(2026, 3, 14, 0, 31, tzinfo=ZoneInfo("Asia/Shanghai"))
    db = pg_db_factory()
    try:
        result = run_dispatch_scheduled(db, container=_container.get_container(), now=now)
    finally:
        db.close()

    assert result["scheduled"] is True
    assert result["target_date"] == "2026-03-13"

    dispatcher = _container.get_container().dispatcher
    assert isinstance(dispatcher, FakeTaskDispatcher)
    assert len(dispatcher.dispatched_daily_summary) == 1
    assert dispatcher.dispatched_daily_summary[0].target_date_str == "2026-03-13"


@pytest.mark.postgres
def test_concurrent_schedulers_publish_one_daily_summary_task(
    pg_db_factory: SessionFactory,
) -> None:
    """Two threads racing the per-date ``AppRuntimeState`` claim produce exactly one dispatch.

    The unique constraint on ``app_runtime_state.state_key`` is the
    concurrency safety net: the first claim wins, the second sees the
    row already present and returns ``False``. The
    :class:`FakeTaskDispatcher` therefore records exactly one
    ``dispatch_generate_daily_summary`` call across both threads.

    The threads call the public orchestration entry
    :func:`run_dispatch_scheduled` directly so the deterministic
    ``now`` argument produces a stable target date across both calls
    (the Celery wrapper hard-codes ``home_now(zone)`` which we cannot
    monkeypatch in this batch).
    """

    db = pg_db_factory()
    try:
        _seed_schedule(db, schedule_text="00:30", zone_name="UTC")
    finally:
        db.close()

    now = datetime(2026, 3, 14, 0, 31, tzinfo=ZoneInfo("UTC"))

    def _run_once() -> dict[str, Any]:
        db = pg_db_factory()
        try:
            return run_dispatch_scheduled(db, container=_container.get_container(), now=now)
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: _run_once(), range(2)))

    assert [item["scheduled"] for item in results].count(True) == 1
    assert any(item.get("reason") == "dispatch_guard_blocked" for item in results)

    dispatcher = _container.get_container().dispatcher
    assert isinstance(dispatcher, FakeTaskDispatcher)
    assert len(dispatcher.dispatched_daily_summary) == 1
    assert dispatcher.dispatched_daily_summary[0].target_date_str == "2026-03-13"


# ---------------------------------------------------------------------------
# Generation — container-seam rewrite (B tests)
# ---------------------------------------------------------------------------


def test_generate_daily_summary_enrolls_one_outbox_per_subscriber(
    pg_db_factory: SessionFactory,
) -> None:
    """Two enabled webhooks subscribed to ``daily_summary_generated`` → two outbox rows.

    The publish use case enrols one ``OutboxEvent`` per subscriber
    directly via :func:`src.application.outbox.enqueue.enqueue_command`
    — the container's :class:`FakeTaskDispatcher` never receives a
    ``dispatch_webhook`` call because the outbox publisher (a separate
    process) is what later drives that path. The observable contract
    is two ``outbox_event`` rows with ``webhook_id`` matching the
    seeded ``WebhookConfig`` ids.
    """

    db = pg_db_factory()
    try:
        _seed_qa_provider(db)
        source_id, session_id = _create_source_and_session(
            db,
            session_start_time=datetime(2026, 3, 13, 0, 0, 0, tzinfo=timezone.utc),
            session_end_time=datetime(2026, 3, 13, 23, 59, 59, tzinfo=timezone.utc),
        )

        db.add_all(
            [
                WebhookConfig(
                    name="hook-one",
                    url="https://example.com/hook-one",
                    event_subscriptions_json=[{"event": "daily_summary_generated", "version": ""}],
                    enabled=True,
                ),
                WebhookConfig(
                    name="hook-two",
                    url="https://example.com/hook-two",
                    event_subscriptions_json=[{"event": "daily_summary_generated", "version": ""}],
                    enabled=True,
                ),
            ]
        )
        db.add(
            EventRecord(
                source_id=source_id,
                session_id=session_id,
                event_start_time=datetime(2026, 3, 13, 9, 0, 0, tzinfo=timezone.utc),
                description="成员出现在客厅",
                event_type="member_appear",
                title="成员出现",
                summary="爸爸上午出现在客厅并活动",
            )
        )
        db.commit()
    finally:
        db.close()

    response_payload = {
        "overall_summary": "昨天家中整体平稳，成员在客厅有活动。",
        "subject_sections": [
            {
                "subject_name": "爸爸",
                "subject_type": "member",
                "summary": "上午在客厅活动较多。",
                "attention_needed": False,
            }
        ],
        "attention_items": [],
    }
    _set_text_responses(json.dumps(response_payload, ensure_ascii=False))

    result = summarizer.generate_daily_summary_task.run("2026-03-13")

    assert result["summary_date"] == "2026-03-13"
    assert len(_latest_gateway().calls) == 1

    verify_db = pg_db_factory()
    try:
        outbox_rows = verify_db.query(OutboxEvent).all()
        assert len(outbox_rows) == 2
        targeted_webhook_ids = sorted(int(row.kwargs_json["webhook_id"]) for row in outbox_rows)
        assert targeted_webhook_ids == [1, 2]
        for row in outbox_rows:
            assert row.task_name == "src.tasks.webhook.send_webhook_task"
            assert row.kwargs_json["event_type"] == "daily_summary_generated"
            assert row.status == OutboxStatus.PENDING.value

        dispatcher = _container.get_container().dispatcher
        assert isinstance(dispatcher, FakeTaskDispatcher)
        # The publish use case enrolls outbox rows directly; the
        # fake dispatcher never receives a ``dispatch_webhook`` call
        # during the generation flow — the outbox publisher (a
        # separate process) is what later drives that path.
        assert dispatcher.dispatched_webhook == []
    finally:
        verify_db.close()


def test_generate_daily_summary_uses_single_pass_under_threshold(
    pg_db_factory: SessionFactory,
) -> None:
    """Natural data input is below the threshold → orchestrator drives the single-pass path.

    The :class:`~src.services.prompt_builder.compression.daily_summary_compressor`
    caps the data input at :data:`MAX_DATA_INPUT_PROMPT_CHARS` (12000)
    so a single seeded event with a short description leaves the
    user prompt well below :data:`SERIAL_SPLIT_PROMPT_THRESHOLD`
    (28000). The single-pass branch issues exactly one
    ``chat_completion`` call and persists the parsed summary.
    """

    db = pg_db_factory()
    try:
        _seed_qa_provider(db)
        source_id, session_id = _create_source_and_session(
            db,
            session_start_time=datetime(2026, 3, 13, 0, 0, 0, tzinfo=timezone.utc),
            session_end_time=datetime(2026, 3, 13, 23, 59, 59, tzinfo=timezone.utc),
        )
        db.add(
            HomeEntityProfile(
                entity_type="member",
                name="爸爸",
                role_type="father",
                age_group="adult",
                is_enabled=True,
                sort_order=0,
            )
        )
        db.add(
            EventRecord(
                source_id=source_id,
                session_id=session_id,
                event_start_time=datetime(2026, 3, 13, 9, 0, 0, tzinfo=timezone.utc),
                description="成员出现在客厅",
                event_type="member_appear",
                title="成员出现",
                summary="爸爸上午出现在客厅并活动",
                related_entities_json=[
                    {
                        "entity_type": "member",
                        "display_name": "爸爸",
                        "matched_profile_name": "爸爸",
                        "recognition_status": "confirmed",
                    }
                ],
            )
        )
        db.commit()
    finally:
        db.close()

    response_payload = {
        "overall_summary": "昨天爸爸在客厅有活动，整体平稳。",
        "subject_sections": [
            {
                "subject_name": "爸爸",
                "subject_type": "member",
                "summary": "爸爸上午在客厅活动。",
                "attention_needed": False,
            }
        ],
        "attention_items": [],
    }
    _set_text_responses(json.dumps(response_payload, ensure_ascii=False))

    result = summarizer.generate_daily_summary_task.run("2026-03-13")

    assert result["summary_date"] == "2026-03-13"
    # Single-pass path → exactly one LLM call.
    assert len(_latest_gateway().calls) == 1

    verify_db = pg_db_factory()
    try:
        summary = (
            verify_db.query(DailySummary)
            .filter(DailySummary.summary_date == datetime(2026, 3, 13).date())
            .first()
        )
        assert summary is not None
        assert summary.overall_summary.startswith("昨天爸爸在客厅有活动")
        subject_sections = summary.subject_sections_json or []
        assert any(section.get("subject_name") == "爸爸" for section in subject_sections)
    finally:
        verify_db.close()


def test_generate_daily_summary_uses_serial_path_when_threshold_is_overridden(
    pg_db_factory: SessionFactory,
) -> None:
    """Threshold override → orchestrator drives the serial (per-subject + rollup) path.

    The natural data-input cap (``MAX_DATA_INPUT_PROMPT_CHARS``) keeps
    the single-pass user prompt bounded at ~12700 chars, well below
    the production :data:`SERIAL_SPLIT_PROMPT_THRESHOLD` (28000), so
    the serial branch is unreachable from the Celery task wrapper
    alone. The orchestrator's :func:`run_daily_summary_generation`
    entry exposes an optional ``serial_split_prompt_threshold`` kwarg
    (mirroring the :class:`~src.application.qa.use_case_query.AnswerQuestionUseCase`
    optional-port pattern) so the test seam can drive the serial path
    without monkey-patching the private constant. The serial path
    issues one ``chat_completion`` per subject plus one rollup call —
    two calls for the two seeded subjects (``爸爸`` + ``布丁``).
    """

    db = pg_db_factory()
    try:
        _seed_qa_provider(db)
        source_id, session_id = _create_source_and_session(
            db,
            session_start_time=datetime(2026, 3, 13, 0, 0, 0, tzinfo=timezone.utc),
            session_end_time=datetime(2026, 3, 13, 23, 59, 59, tzinfo=timezone.utc),
        )
        db.add_all(
            [
                HomeEntityProfile(
                    entity_type="member",
                    name="爸爸",
                    role_type="father",
                    age_group="adult",
                    is_enabled=True,
                    sort_order=0,
                ),
                HomeEntityProfile(
                    entity_type="pet",
                    name="布丁",
                    role_type="cat",
                    is_enabled=True,
                    sort_order=1,
                ),
                EventRecord(
                    source_id=source_id,
                    session_id=session_id,
                    event_start_time=datetime(2026, 3, 13, 9, 0, 0, tzinfo=timezone.utc),
                    description="成员出现在客厅",
                    event_type="member_appear",
                    title="成员出现",
                    summary="爸爸上午出现在客厅并活动",
                    related_entities_json=[
                        {
                            "entity_type": "member",
                            "display_name": "爸爸",
                            "matched_profile_name": "爸爸",
                            "recognition_status": "confirmed",
                        }
                    ],
                ),
                EventRecord(
                    source_id=source_id,
                    session_id=session_id,
                    event_start_time=datetime(2026, 3, 13, 14, 0, 0, tzinfo=timezone.utc),
                    description="布丁在沙发",
                    event_type="pet_appear",
                    title="宠物出现",
                    summary="布丁下午在沙发",
                    related_entities_json=[
                        {
                            "entity_type": "pet",
                            "display_name": "布丁",
                            "matched_profile_name": "布丁",
                            "recognition_status": "confirmed",
                        }
                    ],
                ),
            ]
        )
        db.commit()
    finally:
        db.close()

    subject_response = json.dumps(
        {"summary": "上午在客厅活动。", "attention_needed": False},
        ensure_ascii=False,
    )
    rollup_response = json.dumps(
        {"overall_summary": "昨天爸爸和布丁都在家，整体平稳。", "attention_items": []},
        ensure_ascii=False,
    )
    gateway = _install_branching_gateway(
        subject_response=subject_response,
        rollup_response=rollup_response,
    )

    # Build the TaskLog the public orchestration entry expects; the
    # serial-path test bypasses the Celery wrapper so we must supply
    # the row the wrapper would normally bind via
    # ``bind_or_create_running_task_log``.
    db = pg_db_factory()
    try:
        task_log = TaskLog(
            task_type="daily_summary_generation",
            task_target_id=None,
            queue_task_id=None,
            status="running",
            detail_json={
                "target_date": "2026-03-13",
                "dedupe_key": "daily_summary_generation|2026-03-13",
            },
        )
        db.add(task_log)
        db.flush()
        task_log_id = int(task_log.id)
        db.commit()
    finally:
        db.close()

    db = pg_db_factory()
    try:
        task_log = db.query(TaskLog).filter(TaskLog.id == task_log_id).one()
        result = run_daily_summary_generation(
            db=db,
            target_date=datetime(2026, 3, 13).date(),
            queue_task_id=None,
            container=_container.get_container(),
            task_log=task_log,
            serial_split_prompt_threshold=1,
        )
        db.commit()
    finally:
        db.close()

    assert result["summary_date"] == "2026-03-13"
    # Serial path → 1 subject call per known subject + 1 rollup call.
    # Two known subjects (``爸爸`` + ``布丁``) → 3 total LLM calls.
    assert len(gateway.calls) == 3
    subject_calls = [
        c for c in gateway.calls if "对象摘要任务" in str(c["messages"][-1]["content"])
    ]
    rollup_calls = [
        c for c in gateway.calls if "对象摘要任务" not in str(c["messages"][-1]["content"])
    ]
    assert len(subject_calls) == 2
    assert len(rollup_calls) == 1

    verify_db = pg_db_factory()
    try:
        summary = (
            verify_db.query(DailySummary)
            .filter(DailySummary.summary_date == datetime(2026, 3, 13).date())
            .first()
        )
        assert summary is not None
        assert summary.overall_summary == "昨天爸爸和布丁都在家，整体平稳。"
    finally:
        verify_db.close()


# ---------------------------------------------------------------------------
# Suppress unused import warning for ``timedelta`` (kept for future use).
# ---------------------------------------------------------------------------

_ = timedelta
