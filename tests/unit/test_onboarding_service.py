from sqlalchemy.orm import Session

from src.models.home_profile import HomeProfile
from src.models.llm_provider import LLMProvider
from src.models.system_config import SystemConfig
from src.models.video_source import VideoSource
from src.services.onboarding import get_onboarding_status


def test_onboarding_status_basic_not_ready_when_empty(pg_db: Session) -> None:
    status = get_onboarding_status(pg_db)
    assert status["overall_status"] == "basic_not_ready"
    assert status["basic_ready"] is False
    assert status["full_ready"] is False


def test_onboarding_status_basic_ready(pg_db: Session) -> None:
    pg_db.add(
        VideoSource(
            source_name="客厅",
            camera_name="cam-1",
            location_name="客厅",
            source_type="local_directory",
            config_json={"root_path": "/data/videos"},
            enabled=True,
            last_validate_status="success",
        )
    )
    pg_db.add(
        LLMProvider(
            provider_name="默认 Provider",
            provider_type="qa_provider",
            api_base_url="https://example.com/v1",
            api_key="x",
            model_name="test-model",
            enabled=True,
            supports_vision=True,
            supports_qa=True,
            is_default_vision=True,
            is_default_qa=True,
            last_test_status="success",
        )
    )
    pg_db.commit()

    status = get_onboarding_status(pg_db)
    assert status["overall_status"] == "basic_ready"
    assert status["basic_ready"] is True
    assert status["full_ready"] is False


def test_onboarding_status_full_ready(pg_db: Session) -> None:
    pg_db.add(
        VideoSource(
            source_name="客厅",
            camera_name="cam-1",
            location_name="客厅",
            source_type="local_directory",
            config_json={"root_path": "/data/videos"},
            enabled=True,
            last_validate_status="success",
            description="主要拍客厅",
        )
    )
    pg_db.add(
        LLMProvider(
            provider_name="默认 Provider",
            provider_type="qa_provider",
            api_base_url="https://example.com/v1",
            api_key="x",
            model_name="test-model",
            enabled=True,
            supports_vision=True,
            supports_qa=True,
            is_default_vision=True,
            is_default_qa=True,
            last_test_status="success",
        )
    )
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

    status = get_onboarding_status(pg_db)
    assert status["overall_status"] == "full_ready"
    assert status["basic_ready"] is True
    assert status["full_ready"] is True
