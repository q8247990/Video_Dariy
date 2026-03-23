from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from _pytest.monkeypatch import MonkeyPatch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import src.api.deps as api_deps
import src.db.base  # noqa: F401
import src.db.session as db_session_module
from src.api.v1.endpoints import onboarding, video_sources
from src.core.config import settings
from src.models.home_profile import HomeProfile
from src.models.llm_provider import LLMProvider
from src.models.system_config import SystemConfig
from src.models.video_source import VideoSource


@pytest.fixture
def db() -> Session:
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    VideoSource.__table__.create(bind=engine)
    LLMProvider.__table__.create(bind=engine)
    SystemConfig.__table__.create(bind=engine)
    HomeProfile.__table__.create(bind=engine)
    local_session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    db_session = local_session()
    try:
        yield db_session
    finally:
        db_session.close()


@pytest.fixture
def client(db: Session) -> TestClient:
    app = FastAPI()
    app.include_router(onboarding.router, prefix="/api/v1/onboarding")
    app.include_router(video_sources.router, prefix="/api/v1/video-sources")

    def _override_get_db():
        yield db

    def _override_get_current_user() -> SimpleNamespace:
        return SimpleNamespace(id=1, username="admin")

    app.dependency_overrides[api_deps.get_db] = _override_get_db
    app.dependency_overrides[db_session_module.get_db] = _override_get_db
    app.dependency_overrides[api_deps.get_current_user] = _override_get_current_user

    with TestClient(app) as test_client:
        yield test_client


def _seed_basic_ready(db: Session) -> None:
    db.add(
        VideoSource(
            source_name="客厅源",
            camera_name="客厅摄像头",
            location_name="客厅",
            source_type="local_directory",
            config_json={"root_path": "/data/videos"},
            enabled=True,
            last_validate_status="success",
            last_validate_at=datetime.utcnow(),
        )
    )
    db.add(
        LLMProvider(
            provider_name="默认 Provider",
            provider_type="qa_provider",
            api_base_url="https://example.com/v1",
            api_key="dummy",
            model_name="gpt-4o-mini",
            timeout_seconds=30,
            retry_count=1,
            enabled=True,
            supports_qa=True,
            is_default_qa=True,
            supports_vision=True,
            is_default_vision=True,
            last_test_status="success",
            last_test_at=datetime.utcnow(),
        )
    )
    db.commit()


def _create_sample_video(root: Path) -> None:
    folder = root / "2026031310"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "01M02S_1772091092.mp4").write_bytes(b"test")


def test_onboarding_status_empty(client: TestClient) -> None:
    response = client.get("/api/v1/onboarding/status")
    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 0
    assert body["data"]["overall_status"] == "basic_not_ready"


def test_onboarding_status_reaches_full_ready(client: TestClient, db: Session) -> None:
    _seed_basic_ready(db)
    db.add(SystemConfig(config_key="home_profile_initialized", config_value=True))
    db.add(
        HomeProfile(
            home_name="王先生一家",
            family_tags_json=["has_child"],
            focus_points_json=["member_inout"],
            system_style="family_companion",
            style_preference_text="",
            assistant_name="小护",
            home_note="",
        )
    )
    db.commit()

    response = client.get("/api/v1/onboarding/status")
    assert response.status_code == 200
    body = response.json()
    assert body["data"]["overall_status"] == "full_ready"
    assert body["data"]["basic_ready"] is True
    assert body["data"]["full_ready"] is True


def test_validate_path_outside_root(
    client: TestClient, tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "VIDEO_ROOT_PATH", str(tmp_path / "allowed"))
    outside = tmp_path / "outside"
    outside.mkdir(parents=True, exist_ok=True)

    response = client.post("/api/v1/video-sources/validate-path", json={"path": str(outside)})
    assert response.status_code == 200
    body = response.json()
    assert body["data"]["valid"] is False
    assert "outside" in body["data"]["message"]


def test_video_source_test_updates_validate_status(
    client: TestClient, db: Session, tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "VIDEO_ROOT_PATH", str(tmp_path))
    _create_sample_video(tmp_path)

    source = VideoSource(
        source_name="客厅源",
        camera_name="客厅摄像头",
        location_name="客厅",
        source_type="local_directory",
        config_json={"root_path": str(tmp_path)},
        enabled=True,
    )
    db.add(source)
    db.commit()

    response = client.post(f"/api/v1/video-sources/{source.id}/test")
    assert response.status_code == 200
    body = response.json()
    assert body["data"]["success"] is True
    assert body["data"]["last_validate_status"] == "success"
