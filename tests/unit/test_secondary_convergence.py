"""Todo 22: secondary-complexity convergence unit tests.

Covers the Wave-6 splits that keep each concern independently testable:

* error enum -> HTTP mapping (``src.api.error_status``);
* the shared pagination helper (``src.api.common.paginate``);
* the provider mutation use cases (``src.application.llm_providers``);
* the dashboard query / presenter split (``src.services.dashboard``);
* the QA agent / legacy strategy separation (``src.application.qa``).
"""

from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from src.api.error_status import BusinessCode, status_for_response_code
from src.application.qa.schemas import QARequest
from src.models.llm_provider import LLMProvider


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


def test_paginate_returns_typed_paginated_response(pg_db: Session) -> None:
    from src.api.common import paginate
    from src.schemas.llm_provider import LLMProviderResponse

    pg_db.add_all([_build_provider(name=f"p{i}") for i in range(3)])
    pg_db.commit()

    response = paginate(
        pg_db.query(LLMProvider).order_by(LLMProvider.id),
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


def test_paginate_supports_transform(pg_db: Session) -> None:
    from src.api.common import paginate
    from src.schemas.llm_provider import LLMProviderResponse

    pg_db.add(_build_provider(name="transformable"))
    pg_db.commit()

    def _transform(provider: LLMProvider) -> dict:
        return {**provider.__dict__, "availability_status": "ok", "availability_message": ""}

    response = paginate(
        pg_db.query(LLMProvider),
        page=1,
        page_size=20,
        schema=LLMProviderResponse,
        transform=_transform,
    )
    assert response.data is not None
    assert response.data.list[0].provider_name == "transformable"
    assert response.data.list[0].availability_status == "ok"


def test_provider_create_rejects_missing_capabilities(pg_db: Session) -> None:
    from src.application.llm_providers import create_provider_use_case

    result = create_provider_use_case(
        pg_db,
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
    assert pg_db.query(LLMProvider).count() == 0


def test_provider_update_not_found(pg_db: Session) -> None:
    from src.application.llm_providers import update_provider_use_case

    result = update_provider_use_case(pg_db, 999, {"provider_name": "nope"}, "zh-CN")
    assert result.error_code == 4002


def test_provider_delete_blocks_in_use(pg_db: Session) -> None:
    from src.application.llm_providers import delete_provider_use_case

    provider = _build_provider(
        name="vision-default", supports_vision=True, supports_qa=False, is_default_vision=True
    )
    pg_db.add(provider)
    pg_db.commit()

    result = delete_provider_use_case(pg_db, provider.id, "zh-CN")
    assert result.error_code == 4003
    assert pg_db.query(LLMProvider).filter(LLMProvider.id == provider.id).first() is not None


def test_provider_set_default_rejects_unsupported_capability(pg_db: Session) -> None:
    from src.application.llm_providers import set_default_provider_use_case

    provider = _build_provider(name="qa-only", supports_qa=True, supports_vision=False)
    pg_db.add(provider)
    pg_db.commit()

    result = set_default_provider_use_case(pg_db, provider.id, "vision_provider", "zh-CN")
    assert result.error_code == 4004


def test_provider_disable_blocks_default(pg_db: Session) -> None:
    from src.application.llm_providers import disable_provider_use_case

    provider = _build_provider(name="default-qa", supports_qa=True, is_default_qa=True)
    pg_db.add(provider)
    pg_db.commit()

    result = disable_provider_use_case(pg_db, provider.id, "zh-CN")
    assert result.error_code == 4003


def test_provider_create_sets_legacy_provider_type(pg_db: Session) -> None:
    from src.application.llm_providers import create_provider_use_case
    from src.services.provider_selector import PROVIDER_TYPE_VISION

    result = create_provider_use_case(
        pg_db,
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


def test_dashboard_query_and_presenter_are_separate(pg_db: Session) -> None:
    from src.models.event_record import EventRecord
    from src.models.video_session import VideoSession
    from src.models.video_source import VideoSource
    from src.services.dashboard import queries
    from src.services.dashboard.presenter import DashboardPresenter

    source = VideoSource(
        source_name="s",
        camera_name="客厅",
        location_name="客厅",
        source_type="local_directory",
        enabled=True,
    )
    pg_db.add(source)
    pg_db.flush()
    session = VideoSession(
        source_id=source.id,
        session_start_time=datetime.utcnow() - timedelta(hours=4),
        session_end_time=datetime.utcnow(),
    )
    pg_db.add(session)
    pg_db.flush()
    pg_db.add(
        EventRecord(
            source_id=source.id,
            session_id=session.id,
            event_start_time=datetime.utcnow() - timedelta(hours=1),
            event_end_time=datetime.utcnow() - timedelta(hours=1) + timedelta(minutes=1),
            description="高优先级",
            importance_level="high",
        )
    )
    pg_db.commit()

    today, yesterday, important = queries.event_summary_counts(pg_db)
    summary = DashboardPresenter.event_summary(today, yesterday, important)
    assert summary.important_event_count_24h == 1

    rows = queries.important_event_rows(pg_db)
    important_events = DashboardPresenter.important_events(rows, "zh-CN")
    assert len(important_events) == 1
    assert important_events[0].camera_name == "客厅"


def test_qa_service_routes_to_legacy_strategy_when_no_tool_calling(
    pg_db: Session, monkeypatch
) -> None:
    import src.application.qa.service as service_module
    from src.application.qa.schemas import QAResult

    pg_db.add(_build_provider(name="qa", supports_qa=True, is_default_qa=True))
    pg_db.commit()

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
        pg_db, llm_factory=type("F", (), {"build": lambda *a, **k: FakeGateway()})()
    )
    result = service.answer(
        QARequest(
            question="昨天发生了什么？",
            now=datetime(2026, 3, 10, 8, 0, 0),
            write_query_log=False,
        )
    )
    assert result.answer_text == "legacy"


class FakeGateway:
    supports_tool_calling = False

    def close(self):
        pass


def test_qa_service_routes_to_agent_strategy_when_tool_calling(pg_db: Session, monkeypatch) -> None:
    import src.application.qa.service as service_module
    from src.application.qa.schemas import QAResult

    pg_db.add(_build_provider(name="qa", supports_qa=True, is_default_qa=True))
    pg_db.commit()

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
    service = service_module.QAService(pg_db, llm_factory=factory)
    result = service.answer(
        QARequest(
            question="昨天发生了什么？",
            now=datetime(2026, 3, 10, 8, 0, 0),
            write_query_log=False,
        )
    )
    assert result.answer_text == "agent"


def test_daily_summaries_exposes_patchable_orchestrator_seam() -> None:
    from src.api.v1.endpoints import daily_summaries

    orchestrator = daily_summaries._pipeline_orchestrator
    assert daily_summaries.get_orchestrator() is orchestrator
    assert callable(orchestrator.dispatch_generate_daily_summary)
