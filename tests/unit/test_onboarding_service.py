from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from src.models.home_profile import HomeProfile
from src.models.llm_provider import LLMProvider
from src.models.system_config import SystemConfig
from src.models.video_source import VideoSource
from src.services.onboarding import get_onboarding_status


def _new_db_session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    VideoSource.__table__.create(bind=engine)
    LLMProvider.__table__.create(bind=engine)
    SystemConfig.__table__.create(bind=engine)
    HomeProfile.__table__.create(bind=engine)
    local_session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    return local_session()


def test_onboarding_status_basic_not_ready_when_empty() -> None:
    db = _new_db_session()
    try:
        status = get_onboarding_status(db)
        assert status["overall_status"] == "basic_not_ready"
        assert status["basic_ready"] is False
        assert status["full_ready"] is False
    finally:
        db.close()


def test_onboarding_status_basic_ready() -> None:
    db = _new_db_session()
    try:
        db.add(
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
        db.add(
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
        db.commit()

        status = get_onboarding_status(db)
        assert status["overall_status"] == "basic_ready"
        assert status["basic_ready"] is True
        assert status["full_ready"] is False
    finally:
        db.close()


def test_onboarding_status_full_ready() -> None:
    db = _new_db_session()
    try:
        db.add(
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
        db.add(
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

        status = get_onboarding_status(db)
        assert status["overall_status"] == "full_ready"
        assert status["basic_ready"] is True
        assert status["full_ready"] is True
    finally:
        db.close()
