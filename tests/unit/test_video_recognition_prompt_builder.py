from datetime import datetime

from src.models.video_source import VideoSource
from src.services.prompt_builder.v2.video_recognition import (
    build_strategy_note,
    build_video_recognition_prompt,
)


def test_build_strategy_note_for_xiaomi_nas() -> None:
    note = build_strategy_note("xiaomi_nas_backup", "local_directory")

    assert "变化触发型存储片段" in note
    assert "语义归纳" in note


def test_build_video_recognition_prompt_contains_four_layers() -> None:
    source = VideoSource(
        id=1,
        source_name="米家NAS",
        camera_name="客厅",
        location_name="客厅",
        source_type="local_directory",
        prompt_text="电视屏幕会反光",
    )
    system_prompt, user_prompt = build_video_recognition_prompt(
        {
            "home_context": {
                "home_profile": {
                    "home_name": "我的家庭",
                    "family_tags": ["has_pet"],
                    "focus_points": ["pet_status"],
                    "system_style": "family_companion",
                    "style_preference_text": "简洁",
                    "assistant_name": "家庭助手",
                    "home_note": "夜间较安静",
                },
                "members": [],
                "pets": [],
            },
            "video_source": source,
            "session_context": {
                "session_id": 100,
                "source_id": 1,
                "session_start_time": datetime(2026, 3, 13, 10, 0, 0),
                "session_end_time": datetime(2026, 3, 13, 10, 1, 0),
                "total_duration_seconds": 60,
            },
            "strategy_context": {
                "ingest_type": "xiaomi_nas_backup",
                "source_type": "local_directory",
                "strategy_note": "优先归纳",
            },
        }
    )

    # Output-schema contract: the system prompt must declare the JSON shape the
    # model is required to return (session_summary / analysis_notes / detail).
    assert '"session_summary"' in system_prompt
    assert '"analysis_notes"' in system_prompt
    assert '"detail"' in system_prompt

    # Data passthrough: home profile name, source name, camera/location name,
    # camera note, and the session window (start ISO + duration) must all be
    # rendered verbatim into the user prompt.
    assert "米家NAS" in user_prompt
    assert "客厅" in user_prompt
    assert "电视屏幕会反光" in user_prompt
    assert "我的家庭" in user_prompt
    assert "2026-03-13T10:00:00" in user_prompt
    # Assert the rendered `key=value` form (task.j2 emits `duration={{ total_duration_seconds }}`)
    # rather than the bare numeric value — the surrounding key is the contract, not the number.
    assert "duration=60" in user_prompt
