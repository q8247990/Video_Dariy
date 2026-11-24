"""Video-recognition prompt builder (Jinja2 templates)."""

import math
from datetime import datetime
from typing import Any

from src.services.prompt_builder.engine import render_template
from src.services.video_analysis.enums import (
    ANALYSIS_NOTE_TYPES,
    EVENT_TYPE_DEFINITIONS,
    INTERPRETED_STATES,
    VIDEO_EVENT_TYPES,
)


def _serialize_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()


def build_strategy_note(ingest_type: str, source_type: str) -> str:
    if ingest_type == "xiaomi_nas_backup":
        return (
            "当前 session 来源于变化触发型存储片段；请结合相邻帧变化做语义归纳。"
            "只有出现可观察变化时才生成事件；整段无实质变化时输出空 events，"
            "并在 analysis_notes 中用 no_significant_event 说明。"
        )
    if source_type == "stream":
        return "当前场景接近流式接入，请提高事件抽取阈值，避免噪声事件。"
    return "按常规家庭监控分析策略执行，保持中等事件粒度。"


def _validate_frame_timestamps(input_mode: str, frame_timestamps: list[float]) -> None:
    """校验图像帧模式的帧时间戳列表（在渲染前执行）。

    - ``input_mode == "image"`` 时列表必须非空，否则渲染会产出一个只有表头的空区段，
      属于调用方错误，必须响亮地失败；video 模式允许空列表（不渲染该区段）。
    - 列表中的每一项必须为有限数值，且整体非递减（乱序或非法值是调用方错误）。
    """
    if input_mode == "image" and not frame_timestamps:
        raise ValueError("image input mode requires at least one frame timestamp")

    previous: float | None = None
    for index, value in enumerate(frame_timestamps):
        is_number = not isinstance(value, bool) and isinstance(value, (int, float))
        if not is_number or not math.isfinite(value):
            raise ValueError(
                f"frame timestamp at index {index} must be a finite number, got {value!r}"
            )
        if previous is not None and value < previous:
            raise ValueError(
                "frame timestamps must be non-decreasing, "
                f"index {index} ({value!r}) < index {index - 1} ({previous!r})"
            )
        previous = float(value)


def build_video_recognition_prompt(input_data: dict[str, Any]) -> tuple[str, str]:
    home_context = input_data["home_context"]
    video_source = input_data["video_source"]
    session_context = input_data["session_context"]
    strategy_context = input_data["strategy_context"]
    event_type_list = input_data.get("event_type_list") or sorted(VIDEO_EVENT_TYPES)
    allowed_event_types = set(event_type_list)
    event_type_definitions = [
        item for item in EVENT_TYPE_DEFINITIONS if item["type"] in allowed_event_types
    ] or EVENT_TYPE_DEFINITIONS

    # 图像帧模式可选参数：默认 video，保证现有调用方渲染结果与历史完全一致。
    input_mode = input_data.get("input_mode") or "video"
    frame_timestamps = list(input_data.get("frame_timestamps") or [])
    previous_subjects = list(input_data.get("previous_subjects") or [])
    # 在渲染前校验，image 模式空列表 / 非法列表属于调用方错误，必须响亮失败。
    _validate_frame_timestamps(input_mode, frame_timestamps)
    frame_total = len(frame_timestamps)

    home_profile = home_context.get("home_profile", {})
    members = home_context.get("members", [])
    pets = home_context.get("pets", [])

    def _read_vs(key: str) -> Any:
        if isinstance(video_source, dict):
            return video_source.get(key)
        return getattr(video_source, key, None)

    session_start_iso = _serialize_datetime(session_context.get("session_start_time"))
    session_end_iso = _serialize_datetime(session_context.get("session_end_time"))

    # Byte-stable prefix: the provider checkpoints the end of this message and
    # shares it across conversations, so no per-call value may enter it.
    task_rules_prompt = render_template(
        "video_recognition/task_rules.j2",
        event_type_definitions=event_type_definitions,
        note_type_list=sorted(ANALYSIS_NOTE_TYPES),
        interpreted_state_values=INTERPRETED_STATES,
        input_mode=input_mode,
    )
    system_prompt = "\n\n".join(
        [
            render_template("video_recognition/system_rules.j2", input_mode=input_mode),
            task_rules_prompt,
        ]
    )

    # Per-call values only: rendered after the image/video payload, never
    # before it, so the shared prefix above stays intact.
    continuity_prompt = render_template(
        "video_recognition/continuity.j2",
        previous_subjects=previous_subjects,
    )
    prompt_chunks = [
        render_template(
            "video_recognition/home_context.j2",
            home_profile=home_profile,
            members=members,
            pets=pets,
        ),
        render_template(
            "video_recognition/camera_context.j2",
            source_name=_read_vs("source_name"),
            camera_name=_read_vs("camera_name"),
            location_name=_read_vs("location_name"),
            camera_note=_read_vs("prompt_text"),
        ),
    ]
    if previous_subjects:
        prompt_chunks.append(continuity_prompt)
    prompt_chunks.append(
        render_template(
            "video_recognition/task_context.j2",
            session_context=session_context,
            session_start_time_iso=session_start_iso,
            session_end_time_iso=session_end_iso,
            strategy_context=strategy_context,
            input_mode=input_mode,
            frame_timestamps=frame_timestamps,
            frame_total=frame_total,
        )
    )

    user_prompt = "\n\n".join(prompt_chunks).rstrip()

    return system_prompt, user_prompt
