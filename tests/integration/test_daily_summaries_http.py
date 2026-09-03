from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import src.api.deps as api_deps
import src.db.session as db_session_module
from src.api.v1.endpoints import daily_summaries
from src.models.video_session import VideoSession
from src.models.video_source import VideoSource
from src.services.pipeline_constants import SessionAnalysisStatus


@pytest.fixture
def client(pg_db: Session) -> TestClient:
    app = FastAPI()
    app.include_router(daily_summaries.router, prefix="/api/v1/daily-summaries")

    def _override_get_db():
        yield pg_db

    def _override_get_current_user() -> SimpleNamespace:
        return SimpleNamespace(id=1, username="admin")

    app.dependency_overrides[api_deps.get_db] = _override_get_db
    app.dependency_overrides[db_session_module.get_db] = _override_get_db
    app.dependency_overrides[api_deps.get_current_user] = _override_get_current_user

    with TestClient(app) as test_client:
        yield test_client


def test_generate_all_daily_summaries_route_hits_post_handler(
    client: TestClient, pg_db: Session, monkeypatch
) -> None:
    source = VideoSource(
        source_name="test-source",
        camera_name="test-camera",
        location_name="home",
        source_type="local_directory",
        enabled=True,
    )
    pg_db.add(source)
    pg_db.flush()
    pg_db.add(
        VideoSession(
            source_id=source.id,
            session_start_time=datetime(2026, 3, 10, 8, 0, 0),
            session_end_time=datetime(2026, 3, 10, 8, 30, 0),
            total_duration_seconds=1800,
            analysis_status=SessionAnalysisStatus.SUCCESS,
        )
    )
    pg_db.commit()

    monkeypatch.setattr(
        "src.api.v1.endpoints.daily_summaries._pipeline_orchestrator.dispatch_generate_daily_summary",
        lambda _db, command: f"task-{command.target_date_str}",
    )

    response = client.post("/api/v1/daily-summaries/generate-all")

    assert response.status_code == 200
    payload = response.json()
    assert payload["code"] == 0
    assert payload["data"]["queued_count"] == 1
    assert payload["data"]["target_dates"] == ["2026-03-10"]
