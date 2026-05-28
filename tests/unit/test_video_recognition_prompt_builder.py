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

    # system prompt assertions
    assert "输出 schema" in system_prompt
    assert '"session_summary"' in system_prompt
    assert '"analysis_notes"' in system_prompt
    assert '"detail"' in system_prompt
    assert "命名优先" in system_prompt
    assert "事实优先" in system_prompt
    assert "保守识别" in system_prompt

    # user prompt assertions
    assert "家庭上下文" in user_prompt
    assert "摄像头上下文" in user_prompt
    assert "任务级 Prompt" in user_prompt
    assert "家庭概况" in user_prompt
    assert "会话上下文" in user_prompt
