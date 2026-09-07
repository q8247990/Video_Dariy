from datetime import datetime
from pathlib import Path

import pytest
from _pytest.monkeypatch import MonkeyPatch
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from src.api.v1.endpoints import onboarding, video_sources
from src.core.config import settings
from src.models.home_profile import HomeProfile
from src.models.llm_provider import LLMProvider
from src.models.system_config import SystemConfig
from src.models.video_source import VideoSource


@pytest.fixture
def client(pg_db: Session, make_http_client) -> TestClient:
    with make_http_client(
        [
            (onboarding.router, "/api/v1/onboarding"),
            (video_sources.router, "/api/v1/video-sources"),
        ],
    ) as test_client:
        yield test_client


def _seed_basic_ready(pg_db: Session) -> None:
    pg_db.add(
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
    pg_db.add(
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
    pg_db.commit()


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


def test_onboarding_status_reaches_full_ready(client: TestClient, pg_db: Session) -> None:
    _seed_basic_ready(pg_db)
    pg_db.add(SystemConfig(config_key="home_profile_initialized", config_value=True))
    pg_db.add(
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
    pg_db.commit()

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
    client: TestClient, pg_db: Session, tmp_path: Path, monkeypatch: MonkeyPatch
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
    pg_db.add(source)
    pg_db.commit()

    response = client.post(f"/api/v1/video-sources/{source.id}/test")
    assert response.status_code == 200
    body = response.json()
    assert body["data"]["success"] is True
    assert body["data"]["last_validate_status"] == "success"
