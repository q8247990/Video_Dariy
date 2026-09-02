"""Todo 22: secondary-complexity convergence unit tests.

Covers the Wave-6 splits that keep each concern independently testable:

* error enum -> HTTP mapping (``src.api.error_status``);
* the shared pagination helper (``src.api.common.paginate``);
* the provider mutation use cases (``src.application.llm_providers``);
* the dashboard query / presenter split (``src.services.dashboard``);
* the QA agent / legacy strategy separation (``src.application.qa``).
"""

from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from src.api.error_status import BusinessCode, status_for_response_code
from src.application.qa.schemas import QARequest
from src.models.llm_provider import LLMProvider
from src.models.system_config import SystemConfig


def _new_db_session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    LLMProvider.__table__.create(bind=engine)
    SystemConfig.__table__.create(bind=engine)
    local_session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    return local_session()


def _build_provider(
    *,
    name: str = "p",
    supports_vision: bool = False,
    supports_qa: bool = True,
    is_default_vision: bool = False,
    is_default_qa: bool = False,
    enabled: bool = True,
) -> LLMProvider:
    return LLMProvider(
        provider_name=name,
        provider_type="qa_provider" if supports_qa and not supports_vision else "vision_provider",
        api_base_url="https://example.com/v1",
        api_key="dummy",
        model_name="gpt-4o-mini",
        timeout_seconds=30,
        retry_count=1,
        enabled=enabled,
        supports_vision=supports_vision,
        supports_qa=supports_qa,
        is_default_vision=is_default_vision,
        is_default_qa=is_default_qa,
    )


def test_error_enum_maps_all_contract_business_codes() -> None:
    expected = {
        4000: 400,
        4001: 409,
        4002: 404,
        4004: 409,
        4011: 401,
        4290: 429,
        4291: 429,
        5000: 500,
        5001: 502,
        5002: 503,
    }
    for code, http_status in expected.items():
        assert status_for_response_code(code) == http_status
        assert BusinessCode(code) == code


def test_error_enum_keeps_http_unchanged_for_unmapped_codes() -> None:
    for code in (4003, 4005, 5003):
        assert status_for_response_code(code) is None


def test_paginate_returns_typed_paginated_response() -> None:
    from src.api.common import paginate
    from src.schemas.llm_provider import LLMProviderResponse

    db = _new_db_session()
    try:
        db.add_all([_build_provider(name=f"p{i}") for i in range(3)])
        db.commit()

        response = paginate(
            db.query(LLMProvider).order_by(LLMProvider.id),
            page=1,
            page_size=2,
            schema=LLMProviderResponse,
        )
        assert response.code == 0
        assert response.data is not None
        assert response.data.pagination.total == 3
        assert response.data.pagination.page == 1
        assert response.data.pagination.page_size == 2
        assert len(response.data.list) == 2
    finally:
        db.close()


def test_paginate_supports_transform() -> None:
    from src.api.common import paginate
    from src.schemas.llm_provider import LLMProviderResponse

    db = _new_db_session()
    try:
        db.add(_build_provider(name="transformable"))
        db.commit()

        def _transform(provider: LLMProvider) -> dict:
            return {**provider.__dict__, "availability_status": "ok", "availability_message": ""}

        response = paginate(
            db.query(LLMProvider),
            page=1,
            page_size=20,
            schema=LLMProviderResponse,
            transform=_transform,
        )
        assert response.data is not None
        assert response.data.list[0].provider_name == "transformable"
        assert response.data.list[0].availability_status == "ok"
    finally:
        db.close()


def test_provider_create_rejects_missing_capabilities() -> None:
    from src.application.llm_providers import create_provider_use_case

    db = _new_db_session()
    try:
        result = create_provider_use_case(
            db,
            {
                "provider_name": "x",
                "api_base_url": "https://example.com/v1",
                "api_key": "dummy",
                "model_name": "m",
                "timeout_seconds": 30,
                "retry_count": 1,
                "enabled": True,
                "supports_vision": False,
                "supports_qa": False,
            },
            "zh-CN",
        )
        assert result.error_code == 4001
        assert db.query(LLMProvider).count() == 0
    finally:
        db.close()


def test_provider_update_not_found() -> None:
    from src.application.llm_providers import update_provider_use_case

    db = _new_db_session()
    try:
        result = update_provider_use_case(db, 999, {"provider_name": "nope"}, "zh-CN")
        assert result.error_code == 4002
    finally:
        db.close()


def test_provider_delete_blocks_in_use() -> None:
    from src.application.llm_providers import delete_provider_use_case

    db = _new_db_session()
    try:
        provider = _build_provider(
            name="vision-default", supports_vision=True, supports_qa=False, is_default_vision=True
        )
        db.add(provider)
        db.commit()

        result = delete_provider_use_case(db, provider.id, "zh-CN")
        assert result.error_code == 4003
        assert db.query(LLMProvider).filter(LLMProvider.id == provider.id).first() is not None
    finally:
        db.close()


def test_provider_set_default_rejects_unsupported_capability() -> None:
    from src.application.llm_providers import set_default_provider_use_case

    db = _new_db_session()
    try:
        provider = _build_provider(name="qa-only", supports_qa=True, supports_vision=False)
        db.add(provider)
        db.commit()

        result = set_default_provider_use_case(db, provider.id, "vision_provider", "zh-CN")
        assert result.error_code == 4004
    finally:
        db.close()


def test_provider_disable_blocks_default() -> None:
    from src.application.llm_providers import disable_provider_use_case

    db = _new_db_session()
    try:
        provider = _build_provider(name="default-qa", supports_qa=True, is_default_qa=True)
        db.add(provider)
        db.commit()

        result = disable_provider_use_case(db, provider.id, "zh-CN")
        assert result.error_code == 4003
    finally:
        db.close()


def test_provider_create_sets_legacy_provider_type() -> None:
    from src.application.llm_providers import create_provider_use_case
    from src.services.provider_selector import PROVIDER_TYPE_VISION

    db = _new_db_session()
    try:
        result = create_provider_use_case(
            db,
            {
                "provider_name": "vision",
                "api_base_url": "https://example.com/v1",
                "api_key": "dummy",
                "model_name": "m",
                "timeout_seconds": 30,
                "retry_count": 1,
                "enabled": True,
                "supports_vision": True,
                "supports_qa": False,
            },
            "zh-CN",
        )
        assert result.error_code == 0
        assert result.provider is not None
        assert result.provider.provider_type == PROVIDER_TYPE_VISION
    finally:
        db.close()


def test_dashboard_query_and_presenter_are_separate() -> None:
    from datetime import timedelta

    from src.models.event_record import EventRecord
    from src.models.video_source import VideoSource
    from src.services.dashboard import queries
    from src.services.dashboard.presenter import DashboardPresenter

    engine = create_engine("sqlite+pysqlite:///:memory:")
    VideoSource.__table__.create(bind=engine)
    EventRecord.__table__.create(bind=engine)
    db = sessionmaker(bind=engine, autocommit=False, autoflush=False)()
    try:
        source = VideoSource(
            source_name="s",
            camera_name="客厅",
            location_name="客厅",
            source_type="local_directory",
            enabled=True,
        )
        db.add(source)
        db.flush()
        db.add(
            EventRecord(
                source_id=source.id,
                session_id=1,
                event_start_time=datetime.utcnow() - timedelta(hours=1),
                event_end_time=datetime.utcnow() - timedelta(hours=1) + timedelta(minutes=1),
                description="高优先级",
                importance_level="high",
            )
        )
        db.commit()

        today, yesterday, important = queries.event_summary_counts(db)
        summary = DashboardPresenter.event_summary(today, yesterday, important)
        assert summary.important_event_count_24h == 1

        rows = queries.important_event_rows(db)
        important_events = DashboardPresenter.important_events(rows, "zh-CN")
        assert len(important_events) == 1
        assert important_events[0].camera_name == "客厅"
    finally:
        db.close()


def test_qa_service_routes_to_legacy_strategy_when_no_tool_calling(monkeypatch) -> None:
    import src.application.qa.service as service_module
    from src.application.qa.schemas import QAResult

    db = _new_db_session()
    try:
        db.add(_build_provider(name="qa", supports_qa=True, is_default_qa=True))
        db.commit()

        class FakeLegacy:
            def __init__(self, db, gateway, provider):
                self.provider = provider

            def execute(self, question, request):
                return QAResult(question=question, answer_text="legacy")

        class FakeAgent:
            def __init__(self, db, gateway, provider):
                raise AssertionError("agent strategy should not be built")

        monkeypatch.setattr(service_module, "LegacyQAStrategy", FakeLegacy)
        monkeypatch.setattr(service_module, "AgentQAStrategy", FakeAgent)

        service = service_module.QAService(
            db, llm_factory=type("F", (), {"build": lambda *a, **k: FakeGateway()})()
        )
        result = service.answer(
            QARequest(
                question="昨天发生了什么？",
                now=datetime(2026, 3, 10, 8, 0, 0),
                write_query_log=False,
            )
        )
        assert result.answer_text == "legacy"
    finally:
        db.close()


class FakeGateway:
    supports_tool_calling = False

    def close(self):
        pass


def test_qa_service_routes_to_agent_strategy_when_tool_calling(monkeypatch) -> None:
    import src.application.qa.service as service_module
    from src.application.qa.schemas import QAResult

    db = _new_db_session()
    try:
        db.add(_build_provider(name="qa", supports_qa=True, is_default_qa=True))
        db.commit()

        class FakeAgent:
            def __init__(self, db, gateway, provider):
                self.provider = provider

            def execute(self, question, request):
                return QAResult(question=question, answer_text="agent")

        class FakeLegacy:
            def __init__(self, db, gateway, provider):
                raise AssertionError("legacy strategy should not be built")

        monkeypatch.setattr(service_module, "AgentQAStrategy", FakeAgent)
        monkeypatch.setattr(service_module, "LegacyQAStrategy", FakeLegacy)

        class ToolCallingGateway(FakeGateway):
            supports_tool_calling = True

        factory = type("F", (), {"build": lambda *a, **k: ToolCallingGateway()})()
        service = service_module.QAService(db, llm_factory=factory)
        result = service.answer(
            QARequest(
                question="昨天发生了什么？",
                now=datetime(2026, 3, 10, 8, 0, 0),
                write_query_log=False,
            )
        )
        assert result.answer_text == "agent"
    finally:
        db.close()


def test_daily_summaries_exposes_patchable_orchestrator_seam() -> None:
    from src.api.v1.endpoints import daily_summaries

    orchestrator = daily_summaries._pipeline_orchestrator
    assert daily_summaries.get_orchestrator() is orchestrator
    assert callable(orchestrator.dispatch_generate_daily_summary)
