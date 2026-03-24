from datetime import date
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import src.api.deps as api_deps
import src.db.base  # noqa: F401
import src.db.session as db_session_module
from src.api.v1.endpoints import llm_providers
from src.db.base_class import Base
from src.models.chat_query_log import ChatQueryLog
from src.models.daily_summary import DailySummary
from src.models.llm_provider import LLMProvider
from src.models.llm_usage_log import LLMUsageLog


@pytest.fixture
def db() -> Session:
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, connection_record) -> None:  # type: ignore[no-untyped-def]
        del connection_record
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(bind=engine)
    local_session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    db_session = local_session()
    try:
        yield db_session
    finally:
        db_session.close()


@pytest.fixture
def client(db: Session) -> TestClient:
    app = FastAPI()
    app.include_router(llm_providers.router, prefix="/api/v1/providers")

    def _override_get_db():
        yield db

    def _override_get_current_user() -> SimpleNamespace:
        return SimpleNamespace(id=1, username="admin")

    app.dependency_overrides[api_deps.get_db] = _override_get_db
    app.dependency_overrides[db_session_module.get_db] = _override_get_db
    app.dependency_overrides[api_deps.get_current_user] = _override_get_current_user

    with TestClient(app) as test_client:
        yield test_client


def _build_provider(
    *,
    name: str,
    enabled: bool = True,
    supports_vision: bool = False,
    supports_qa: bool = True,
    is_default_vision: bool = False,
    is_default_qa: bool = False,
) -> LLMProvider:
    return LLMProvider(
        provider_name=name,
        provider_type="vision_provider" if supports_vision and not supports_qa else "qa_provider",
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


def test_delete_provider_blocks_current_in_use_provider(client: TestClient, db: Session) -> None:
    provider = _build_provider(name="vision-fallback", supports_vision=True, supports_qa=False)
    db.add(provider)
    db.commit()

    response = client.delete(f"/api/v1/providers/{provider.id}")

    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 4004
    assert body["message"] == "Provider is currently in use by: vision"
    assert db.query(LLMProvider).filter(LLMProvider.id == provider.id).first() is not None


def test_delete_provider_clears_historical_references_for_non_current_provider(
    client: TestClient, db: Session
) -> None:
    active_qa = _build_provider(name="qa-default", supports_qa=True, is_default_qa=True)
    active_vision = _build_provider(
        name="vision-default",
        supports_vision=True,
        supports_qa=False,
        is_default_vision=True,
    )
    target = _build_provider(name="old-provider", supports_vision=True, supports_qa=True)
    db.add_all([active_qa, active_vision, target])
    db.commit()

    db.add(
        LLMUsageLog(
            provider_id=target.id,
            provider_name_snapshot=target.provider_name,
            usage_date=date(2026, 3, 24),
            scene="qa_answer",
            prompt_tokens=10,
            completion_tokens=20,
            total_tokens=30,
        )
    )
    db.add(
        ChatQueryLog(
            user_question="发生了什么？",
            parsed_condition_json={"scene": "qa"},
            answer_text="一切正常",
            referenced_event_ids_json=[1],
            provider_id=target.id,
            provider_name_snapshot=target.provider_name,
        )
    )
    db.add(
        DailySummary(
            summary_date=date(2026, 3, 23),
            summary_title="2026-03-23 家庭日报",
            overall_summary="整体平稳",
            subject_sections_json=[],
            attention_items_json=[],
            event_count=0,
            provider_id=target.id,
            provider_name_snapshot=target.provider_name,
        )
    )
    db.commit()

    response = client.delete(f"/api/v1/providers/{target.id}")

    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 0
    assert db.query(LLMProvider).filter(LLMProvider.id == target.id).first() is None

    usage_log = db.query(LLMUsageLog).one()
    assert usage_log.provider_id is None
    assert usage_log.provider_name_snapshot == "old-provider"

    query_log = db.query(ChatQueryLog).one()
    assert query_log.provider_id is None
    assert query_log.provider_name_snapshot == "old-provider"

    summary = db.query(DailySummary).one()
    assert summary.provider_id is None
    assert summary.provider_name_snapshot == "old-provider"
