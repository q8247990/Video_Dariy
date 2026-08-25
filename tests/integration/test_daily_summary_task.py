import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import src.db.base  # noqa: F401
import src.db.session as db_session_module
import src.tasks.summarizer as summarizer
from src.db.base_class import Base
from src.infrastructure.llm.openai_gateway import OpenAICompatGatewayFactory
from src.models.daily_summary import DailySummary
from src.models.event_record import EventRecord
from src.models.home_entity_profile import HomeEntityProfile
from src.models.llm_provider import LLMProvider
from src.models.system_config import SystemConfig
from src.models.video_session import VideoSession
from src.models.video_source import VideoSource
from src.models.webhook_config import WebhookConfig
from src.services.home_timezone import local_day_bounds


@pytest.fixture
def db_session_factory(monkeypatch):
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    local_session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    monkeypatch.setattr(db_session_module, "SessionLocal", local_session)
    return local_session


def _seed_qa_provider(db: Session) -> None:
    db.add(
        LLMProvider(
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
    )
    db.commit()


def test_generate_daily_summary_empty_events_fallback_and_no_webhook(
    db_session_factory, monkeypatch
) -> None:
    db = db_session_factory()
    try:
        _seed_qa_provider(db)

        webhook_send_calls: list[dict] = []

        def _mock_send_task(*args, **kwargs):
            webhook_send_calls.append({"args": args, "kwargs": kwargs})
            return SimpleNamespace(id="mock-task-id")

        class _FakeGateway:
            def chat_completion(self, messages, temperature=0.2, max_tokens=None):
                return ""

            def get_last_usage(self):
                return None

        monkeypatch.setattr(summarizer.celery_app, "send_task", _mock_send_task)
        monkeypatch.setattr(OpenAICompatGatewayFactory, "build", lambda *a, **k: _FakeGateway())

        result = summarizer.generate_daily_summary_task.run("2026-03-13")

        assert result["summary_date"] == "2026-03-13"
        assert result["event_count"] == 0

        summary = (
            db.query(DailySummary)
            .filter(DailySummary.summary_date == datetime(2026, 3, 13).date())
            .first()
        )
        assert summary is not None
        assert summary.overall_summary == "昨天家中整体较为平稳，未观测到明确的关键活动。"
        assert summary.subject_sections_json == []
        assert summary.attention_items_json == []
        assert webhook_send_calls == []
    finally:
        db.close()


def test_generate_daily_summary_structured_persist_success(db_session_factory, monkeypatch) -> None:
    db = db_session_factory()
    try:
        _seed_qa_provider(db)

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
            HomeEntityProfile(
                entity_type="pet",
                name="布丁",
                role_type="cat",
                is_enabled=True,
                sort_order=1,
            )
        )
        db.add(
            EventRecord(
                source_id=1,
                session_id=1,
                event_start_time=datetime(2026, 3, 13, 9, 0, 0),
                description="成员出现在客厅",
                event_type="member_appear",
                title="成员出现",
                summary="爸爸上午出现在客厅并活动",
                importance_level="medium",
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
        db.add(
            EventRecord(
                source_id=1,
                session_id=1,
                event_start_time=datetime(2026, 3, 13, 10, 0, 0),
                description="门口出现未知人员",
                event_type="unknown_person_appear",
                title="未知人员出现",
                summary="门口出现未知人员短暂停留",
                importance_level="high",
            )
        )
        db.commit()

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

        class _FakeGateway:
            def chat_completion(self, messages, temperature=0.2, max_tokens=None):
                return json.dumps(response_payload, ensure_ascii=False)

            def get_last_usage(self):
                return None

        monkeypatch.setattr(OpenAICompatGatewayFactory, "build", lambda *a, **k: _FakeGateway())

        result = summarizer.generate_daily_summary_task.run("2026-03-13")

        assert result["summary_date"] == "2026-03-13"
        assert result["event_count"] == 2

        summary = (
            db.query(DailySummary)
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
        db.close()


def test_dispatch_daily_summary_runs_once_per_target_date(db_session_factory, monkeypatch) -> None:
    db = db_session_factory()
    try:
        now = datetime.now(timezone.utc)
        db.add(
            SystemConfig(config_key="daily_summary_schedule", config_value=now.strftime("%H:%M"))
        )
        db.add(SystemConfig(config_key="home_timezone", config_value="UTC"))
        db.commit()

        dispatch_calls: list[dict] = []

        def _mock_dispatch_daily_summary(command):
            dispatch_calls.append({"target_date_str": command.target_date_str})
            return "dispatch-task-id"

        monkeypatch.setattr(
            summarizer,
            "_get_pipeline_orchestrator",
            lambda: SimpleNamespace(dispatch_generate_daily_summary=_mock_dispatch_daily_summary),
        )

        first = summarizer.dispatch_scheduled_daily_summary_task.run()
        second = summarizer.dispatch_scheduled_daily_summary_task.run()

        assert first["scheduled"] is True
        assert second["scheduled"] is False
        assert second["reason"] == "dispatch_guard_blocked"
        assert len(dispatch_calls) == 1
    finally:
        db.close()


def test_dispatch_daily_summary_retries_after_dispatch_failure(
    db_session_factory, monkeypatch
) -> None:
    db = db_session_factory()
    try:
        now = datetime.now(timezone.utc)
        db.add(
            SystemConfig(config_key="daily_summary_schedule", config_value=now.strftime("%H:%M"))
        )
        db.add(SystemConfig(config_key="home_timezone", config_value="UTC"))
        db.commit()

        dispatch_calls: list[str] = []

        def _mock_dispatch_daily_summary(command):
            dispatch_calls.append(str(command.target_date_str))
            if len(dispatch_calls) == 1:
                raise RuntimeError("queue unavailable")
            return "dispatch-task-id"

        monkeypatch.setattr(
            summarizer,
            "_get_pipeline_orchestrator",
            lambda: SimpleNamespace(dispatch_generate_daily_summary=_mock_dispatch_daily_summary),
        )

        first = summarizer.dispatch_scheduled_daily_summary_task.run()
        second = summarizer.dispatch_scheduled_daily_summary_task.run()

        assert first["scheduled"] is False
        assert first["reason"] == "dispatch_failed"
        assert second["scheduled"] is True
        assert len(dispatch_calls) == 2
    finally:
        db.close()


def test_dispatch_daily_summary_uses_home_local_schedule_and_date(
    db_session_factory, monkeypatch
) -> None:
    db = db_session_factory()
    try:
        db.add_all(
            [
                SystemConfig(config_key="daily_summary_schedule", config_value="00:30"),
                SystemConfig(config_key="home_timezone", config_value="Asia/Shanghai"),
            ]
        )
        db.commit()
        monkeypatch.setattr(
            summarizer,
            "home_now",
            lambda zone: datetime(2026, 3, 14, 0, 31, tzinfo=zone),
        )
        dispatched_dates: list[str] = []
        monkeypatch.setattr(
            summarizer,
            "_get_pipeline_orchestrator",
            lambda: SimpleNamespace(
                dispatch_generate_daily_summary=lambda command: (
                    dispatched_dates.append(command.target_date_str) or "summary-task"
                )
            ),
        )

        result = summarizer.dispatch_scheduled_daily_summary_task.run()

        assert result["scheduled"] is True
        assert dispatched_dates == ["2026-03-13"]
    finally:
        db.close()


def test_generate_daily_summary_uses_home_timezone_half_open_event_range(
    db_session_factory, monkeypatch
) -> None:
    db = db_session_factory()
    try:
        _seed_qa_provider(db)
        db.add(SystemConfig(config_key="home_timezone", config_value="Asia/Shanghai"))
        db.add_all(
            [
                EventRecord(
                    source_id=1,
                    session_id=1,
                    event_start_time=datetime(2026, 3, 12, 16, tzinfo=timezone.utc),
                    description="local day start",
                ),
                EventRecord(
                    source_id=1,
                    session_id=1,
                    event_start_time=datetime(2026, 3, 13, 15, 59, 59, tzinfo=timezone.utc),
                    description="local day end",
                ),
                EventRecord(
                    source_id=1,
                    session_id=1,
                    event_start_time=datetime(2026, 3, 12, 15, 59, 59, tzinfo=timezone.utc),
                    description="previous local day",
                ),
                EventRecord(
                    source_id=1,
                    session_id=1,
                    event_start_time=datetime(2026, 3, 13, 16, tzinfo=timezone.utc),
                    description="next local day",
                ),
            ]
        )
        db.commit()

        class _FakeGateway:
            def chat_completion(self, messages, temperature=0.2, max_tokens=None):
                return '{"overall_summary":"stable","subject_sections":[],"attention_items":[]}'

            def get_last_usage(self):
                return None

        monkeypatch.setattr(OpenAICompatGatewayFactory, "build", lambda *a, **k: _FakeGateway())

        result = summarizer.generate_daily_summary_task.run("2026-03-13")

        assert result["event_count"] == 2
    finally:
        db.close()


@pytest.mark.postgres
def test_concurrent_schedulers_publish_one_daily_summary_task(postgres_engine, monkeypatch) -> None:
    Base.metadata.create_all(bind=postgres_engine)
    local_session = sessionmaker(bind=postgres_engine, autocommit=False, autoflush=False)
    db = local_session()
    try:
        db.add_all(
            [
                SystemConfig(config_key="daily_summary_schedule", config_value="00:30"),
                SystemConfig(config_key="home_timezone", config_value="UTC"),
            ]
        )
        db.commit()
    finally:
        db.close()

    monkeypatch.setattr(db_session_module, "SessionLocal", local_session)
    monkeypatch.setattr(
        summarizer,
        "home_now",
        lambda zone: datetime(2026, 3, 14, 0, 31, tzinfo=zone),
    )
    dispatches: list[str] = []
    monkeypatch.setattr(
        summarizer,
        "_get_pipeline_orchestrator",
        lambda: SimpleNamespace(
            dispatch_generate_daily_summary=lambda command: (
                dispatches.append(command.target_date_str) or "summary-task"
            )
        ),
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(lambda _: summarizer.dispatch_scheduled_daily_summary_task.run(), range(2))
        )

    assert [item["scheduled"] for item in results].count(True) == 1
    assert dispatches == ["2026-03-13"]


@pytest.mark.postgres
def test_postgres_event_query_maps_new_york_local_day_to_utc_half_open_range(
    postgres_engine,
) -> None:
    Base.metadata.create_all(bind=postgres_engine)
    db = Session(bind=postgres_engine)
    try:
        source = VideoSource(
            source_name="new-york-camera",
            camera_name="living",
            location_name="home",
            source_type="ipcamera",
        )
        db.add(source)
        db.flush()
        session = VideoSession(
            source_id=source.id,
            session_start_time=datetime(2026, 3, 8, 5, tzinfo=timezone.utc),
            session_end_time=datetime(2026, 3, 9, 4, tzinfo=timezone.utc),
        )
        db.add(session)
        db.flush()
        db.add_all(
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
        db.commit()
        start, end = local_day_bounds(ZoneInfo("America/New_York"), datetime(2026, 3, 8).date())

        events = (
            db.query(EventRecord)
            .filter(EventRecord.event_start_time >= start, EventRecord.event_start_time < end)
            .all()
        )

        assert [event.description for event in events] == ["local start"]
    finally:
        db.close()


def test_generate_daily_summary_honors_cancel_requested(db_session_factory, monkeypatch) -> None:
    db = db_session_factory()
    try:
        _seed_qa_provider(db)
        db.add(
            summarizer.TaskLog(
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

    class _FakeGateway:
        def chat_completion(self, messages, temperature=0.2, max_tokens=None):
            return "{}"

        def get_last_usage(self):
            return None

    monkeypatch.setattr(OpenAICompatGatewayFactory, "build", lambda *a, **k: _FakeGateway())

    summarizer.generate_daily_summary_task.push_request(id="cancel-summary-task", retries=0)
    try:
        result = summarizer.generate_daily_summary_task.run("2026-03-13")
    finally:
        summarizer.generate_daily_summary_task.pop_request()

    assert result["cancelled"] is True

    verify_db = db_session_factory()
    try:
        task_log = (
            verify_db.query(summarizer.TaskLog).filter_by(queue_task_id="cancel-summary-task").one()
        )
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


def test_generate_daily_summary_dispatch_webhook_with_legacy_subscription(
    db_session_factory, monkeypatch
) -> None:
    db = db_session_factory()
    try:
        _seed_qa_provider(db)

        db.add(
            WebhookConfig(
                name="legacy-hook",
                url="https://example.com/webhook",
                event_types_json=["daily_summary_generated"],
                event_subscriptions_json=None,
                enabled=True,
            )
        )
        db.add(
            EventRecord(
                source_id=1,
                session_id=1,
                event_start_time=datetime(2026, 3, 13, 9, 0, 0),
                description="成员出现在客厅",
                event_type="member_appear",
                title="成员出现",
                summary="爸爸上午出现在客厅并活动",
                importance_level="medium",
            )
        )
        db.commit()

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

        class _FakeGateway:
            def chat_completion(self, messages, temperature=0.2, max_tokens=None):
                return json.dumps(response_payload, ensure_ascii=False)

            def get_last_usage(self):
                return None

        webhook_calls: list[dict] = []

        def _capture_webhook(command):
            webhook_calls.append({"event_type": command.event_type, "payload": command.payload})
            return "mock-webhook-task"

        monkeypatch.setattr(OpenAICompatGatewayFactory, "build", lambda *a, **k: _FakeGateway())
        monkeypatch.setattr(
            summarizer,
            "_get_pipeline_orchestrator",
            lambda: SimpleNamespace(dispatch_webhook=_capture_webhook),
        )

        result = summarizer.generate_daily_summary_task.run("2026-03-13")

        assert result["summary_date"] == "2026-03-13"
        assert len(webhook_calls) == 1
        assert webhook_calls[0]["event_type"] == "daily_summary_generated"

        payload = webhook_calls[0]["payload"]
        assert payload["event"] == "daily_summary_generated"
        assert payload["version"] == "1.0"
        assert "generated_at" in payload
        assert payload["data"]["date"] == "2026-03-13"
        assert payload["data"]["summary_title"] == "2026-03-13 家庭日报"
    finally:
        db.close()


def test_generate_daily_summary_uses_single_pass_under_threshold(
    db_session_factory, monkeypatch
) -> None:
    db = db_session_factory()
    try:
        _seed_qa_provider(db)
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
                source_id=1,
                session_id=1,
                event_start_time=datetime(2026, 3, 13, 9, 0, 0),
                description="成员出现在客厅",
                event_type="member_appear",
                title="成员出现",
                summary="爸爸上午出现在客厅并活动",
                importance_level="medium",
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

        class _SinglePassGateway:
            def chat_completion(self, messages, temperature=0.2, max_tokens=None):
                return json.dumps(
                    {
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
                    },
                    ensure_ascii=False,
                )

            def get_last_usage(self):
                return None

        monkeypatch.setattr(
            OpenAICompatGatewayFactory, "build", lambda *a, **k: _SinglePassGateway()
        )

        result = summarizer.generate_daily_summary_task.run("2026-03-13")

        assert result["summary_date"] == "2026-03-13"
        task_log = (
            db.query(summarizer.TaskLog)
            .filter(summarizer.TaskLog.task_type == "daily_summary_generation")
            .order_by(summarizer.TaskLog.id.desc())
            .first()
        )
        assert task_log is not None
        detail = task_log.detail_json if isinstance(task_log.detail_json, dict) else {}
        assert detail.get("summary_mode") == "single_pass"
    finally:
        db.close()


def test_generate_daily_summary_uses_split_when_prompt_over_threshold(
    db_session_factory, monkeypatch
) -> None:
    db = db_session_factory()
    try:
        _seed_qa_provider(db)
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
                source_id=1,
                session_id=1,
                event_start_time=datetime(2026, 3, 13, 9, 0, 0),
                description="成员出现在客厅",
                event_type="member_appear",
                title="成员出现",
                summary="爸爸上午出现在客厅并活动",
                importance_level="medium",
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

        class _SplitGateway:
            def chat_completion(self, messages, temperature=0.2, max_tokens=None):
                prompt = str(messages[-1]["content"])
                if "对象摘要任务" in prompt:
                    return json.dumps(
                        {"summary": "爸爸上午在客厅活动。", "attention_needed": False},
                        ensure_ascii=False,
                    )
                return json.dumps(
                    {
                        "overall_summary": "昨天爸爸在客厅有活动，整体平稳。",
                        "attention_items": [],
                    },
                    ensure_ascii=False,
                )

            def get_last_usage(self):
                return None

        monkeypatch.setattr(OpenAICompatGatewayFactory, "build", lambda *a, **k: _SplitGateway())
        monkeypatch.setattr(summarizer, "SERIAL_SPLIT_PROMPT_THRESHOLD", 1)

        result = summarizer.generate_daily_summary_task.run("2026-03-13")

        assert result["summary_date"] == "2026-03-13"
        task_log = (
            db.query(summarizer.TaskLog)
            .filter(summarizer.TaskLog.task_type == "daily_summary_generation")
            .order_by(summarizer.TaskLog.id.desc())
            .first()
        )
        assert task_log is not None
        detail = task_log.detail_json if isinstance(task_log.detail_json, dict) else {}
        assert detail.get("summary_mode") == "split_serial"
    finally:
        db.close()
