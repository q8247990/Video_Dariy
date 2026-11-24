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
    assert "空 events" in note
    assert "no_significant_event" in note
    assert "不要因短暂静止忽略整段价值" not in note


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
    # Assert the rendered `key=value` form (task_context.j2 emits
    # `duration={{ total_duration_seconds }}`) rather than the bare numeric
    # value — the surrounding key is the contract, not the number.
    assert "duration=60" in user_prompt


def _cache_input(
    *,
    session_id: int = 100,
    start_hour: int = 10,
    source_name: str = "米家NAS",
    camera_note: str = "电视屏幕会反光",
    previous_subjects: list[str] | None = None,
    input_mode: str = "video",
    frame_timestamps: list[float] | None = None,
) -> dict:
    data = {
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
        "video_source": {
            "source_name": source_name,
            "camera_name": "客厅",
            "location_name": "客厅",
            "prompt_text": camera_note,
            "source_type": "local_directory",
        },
        "session_context": {
            "session_id": session_id,
            "source_id": 1,
            "session_start_time": datetime(2026, 3, 13, start_hour, 0, 0),
            "session_end_time": datetime(2026, 3, 13, start_hour, 1, 0),
            "total_duration_seconds": 60,
            "segment_index": 0,
            "segment_start_offset_sec": 0,
            "segment_duration_seconds": 60,
        },
        "strategy_context": {
            "ingest_type": "xiaomi_nas_backup",
            "source_type": "local_directory",
            "strategy_note": "优先归纳",
        },
        "previous_subjects": previous_subjects or [],
        "input_mode": input_mode,
        "frame_timestamps": frame_timestamps or [],
    }
    return data


def test_system_prompt_is_byte_identical_across_calls() -> None:
    """Cache contract: everything fixed lives in messages[0], byte for byte.

    Strata keeps a permanent root checkpoint at the end of the system prompt
    and shares it across conversations; a per-call value leaking into that
    message would drop the reuse to zero.
    """
    reference_system, _ = build_video_recognition_prompt(_cache_input())
    variants = [
        _cache_input(session_id=999),
        _cache_input(start_hour=3),
        _cache_input(source_name="阳台摄像头"),
        _cache_input(camera_note="夜间红外"),
        _cache_input(previous_subjects=["嘟嘟", "墨墨"]),
        _cache_input(input_mode="image", frame_timestamps=[0.0, 3.5, 7.0]),
        _cache_input(input_mode="image", frame_timestamps=[1.0]),
    ]

    for variant in variants:
        variant_system, _ = build_video_recognition_prompt(variant)
        # input_mode is a provider-level constant, so only same-mode variants
        # are expected to share the prefix.
        if variant["input_mode"] == "video":
            assert variant_system == reference_system


def test_video_mode_system_prompt_carries_all_fixed_instructions() -> None:
    system_prompt, user_prompt = build_video_recognition_prompt(_cache_input())

    for marker in (
        "event_type 定义",
        "类型边界",
        "analysis_note_type 枚举",
        "interpreted_state 受控词表",
        "最终约束",
    ):
        assert marker in system_prompt
        assert marker not in user_prompt


def test_per_call_context_stays_out_of_the_system_prompt() -> None:
    system_prompt, user_prompt = build_video_recognition_prompt(
        _cache_input(session_id=777, source_name="阳台摄像头", camera_note="夜间红外")
    )

    for value in ("777", "阳台摄像头", "夜间红外", "2026-03-13T10:00:00"):
        assert value in user_prompt
        assert value not in system_prompt


def test_image_mode_system_prefix_is_stable_across_frame_counts() -> None:
    short_system, short_user = build_video_recognition_prompt(
        _cache_input(input_mode="image", frame_timestamps=[0.0])
    )
    long_system, long_user = build_video_recognition_prompt(
        _cache_input(input_mode="image", frame_timestamps=[0.0, 3.5, 7.0, 12.0])
    )

    assert short_system == long_system
    assert "帧 1/1 — t=0.0s" in short_user
    assert "帧 1/4 — t=0.0s" in long_user
    assert "帧 1/1" not in long_system
