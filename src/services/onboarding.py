from typing import Any

from sqlalchemy.orm import Session

from src.models.home_profile import HomeProfile
from src.models.llm_provider import LLMProvider
from src.models.system_config import SystemConfig
from src.models.video_source import VideoSource

DEFAULT_DAILY_SUMMARY_SCHEDULE = "10:00"
DEFAULT_HOME_NAME = "我的家庭"
DEFAULT_SYSTEM_STYLE = "family_companion"
DEFAULT_ASSISTANT_NAME = "家庭助手"


def get_onboarding_status(db: Session) -> dict[str, Any]:
    video_sources = db.query(VideoSource).filter(VideoSource.enabled.is_(True)).all()
    providers = db.query(LLMProvider).filter(LLMProvider.enabled.is_(True)).all()
    profile = db.query(HomeProfile).order_by(HomeProfile.id.asc()).first()
    config_rows = db.query(SystemConfig).all()
    config_map = {row.config_key: row.config_value for row in config_rows}

    video_configured = len(video_sources) > 0
    video_validated = any((item.last_validate_status or "") == "success" for item in video_sources)

    provider_configured = len(providers) > 0
    provider_tested = any((item.last_test_status or "") == "success" for item in providers)

    daily_summary_value = config_map.get("daily_summary_schedule", DEFAULT_DAILY_SUMMARY_SCHEDULE)
    daily_summary_configured = bool(str(daily_summary_value).strip())

    home_profile_initialized = _to_bool(config_map.get("home_profile_initialized", False))
    profile_is_non_default = _profile_non_default(profile)
    home_profile_configured = home_profile_initialized or profile_is_non_default

    camera_notes_total = len(video_sources)
    camera_notes_configured = sum(
        1 for source in video_sources if bool((source.description or "").strip())
    )

    system_style_configured = bool(profile and (profile.system_style or "").strip())
    assistant_name_configured = bool(profile and (profile.assistant_name or "").strip())

    basic_ready = (
        video_configured
        and video_validated
        and provider_configured
        and provider_tested
        and daily_summary_configured
    )
    full_ready = (
        basic_ready
        and home_profile_configured
        and system_style_configured
        and assistant_name_configured
    )

    if full_ready:
        overall_status = "full_ready"
    elif basic_ready:
        overall_status = "basic_ready"
    else:
        overall_status = "basic_not_ready"

    return {
        "overall_status": overall_status,
        "basic_ready": basic_ready,
        "full_ready": full_ready,
        "steps": {
            "video_source": {
                "configured": video_configured,
                "validated": video_validated,
            },
            "provider": {
                "configured": provider_configured,
                "tested": provider_tested,
            },
            "daily_summary": {
                "configured": daily_summary_configured,
            },
            "home_profile": {
                "configured": home_profile_configured,
            },
            "camera_notes": {
                "configured_count": camera_notes_configured,
                "total_count": camera_notes_total,
            },
            "system_style": {
                "configured": system_style_configured,
            },
            "assistant_name": {
                "configured": assistant_name_configured,
            },
        },
        "next_action": _calc_next_action(
            video_configured=video_configured,
            video_validated=video_validated,
            provider_configured=provider_configured,
            provider_tested=provider_tested,
            daily_summary_configured=daily_summary_configured,
            home_profile_configured=home_profile_configured,
            system_style_configured=system_style_configured,
            assistant_name_configured=assistant_name_configured,
            full_ready=full_ready,
        ),
    }


def _calc_next_action(
    *,
    video_configured: bool,
    video_validated: bool,
    provider_configured: bool,
    provider_tested: bool,
    daily_summary_configured: bool,
    home_profile_configured: bool,
    system_style_configured: bool,
    assistant_name_configured: bool,
    full_ready: bool,
) -> str:
    if not video_configured or not video_validated:
        return "configure_video_source"
    if not provider_configured or not provider_tested:
        return "configure_provider"
    if not daily_summary_configured:
        return "configure_daily_summary"
    if not home_profile_configured:
        return "configure_home_profile"
    if not system_style_configured:
        return "configure_system_style"
    if not assistant_name_configured:
        return "configure_assistant_name"
    if full_ready:
        return "done"
    return "review_settings"


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(value, (int, float)):
        return bool(value)
    return False


def _profile_non_default(profile: HomeProfile | None) -> bool:
    if profile is None:
        return False

    if (profile.home_name or "").strip() and profile.home_name != DEFAULT_HOME_NAME:
        return True
    if profile.family_tags_json:
        return True
    if profile.focus_points_json:
        return True
    if (profile.home_note or "").strip():
        return True
    if (profile.style_preference_text or "").strip():
        return True
    if (profile.system_style or "").strip() and profile.system_style != DEFAULT_SYSTEM_STYLE:
        return True
    if (profile.assistant_name or "").strip() and profile.assistant_name != DEFAULT_ASSISTANT_NAME:
        return True

    return False
