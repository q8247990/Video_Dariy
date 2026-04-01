"""v2: 视频识别 prompt 构建器（Jinja2 模板版）。"""

from datetime import datetime
from typing import Any

from src.services.prompt_builder.engine import render_template
from src.services.video_analysis.enums import ANALYSIS_NOTE_TYPES, VIDEO_EVENT_TYPES


def _serialize_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()


def build_strategy_note(ingest_type: str, source_type: str) -> str:
    if ingest_type == "xiaomi_nas_backup":
        return (
            "当前 session 来源于变化触发型存储片段，通常具备分析价值；"
            "请优先做语义归纳，不要因短暂静止忽略整段价值。"
        )
    if source_type == "stream":
        return "当前场景接近流式接入，请提高事件抽取阈值，避免噪声事件。"
    return "按常规家庭监控分析策略执行，保持中等事件粒度。"


def build_video_recognition_prompt(input_data: dict[str, Any]) -> tuple[str, str]:
    home_context = input_data["home_context"]
    video_source = input_data["video_source"]
    session_context = input_data["session_context"]
    strategy_context = input_data["strategy_context"]
    event_type_list = input_data.get("event_type_list") or sorted(VIDEO_EVENT_TYPES)

    home_profile = home_context.get("home_profile", {})
    members = home_context.get("members", [])
    pets = home_context.get("pets", [])

    def _read_vs(key: str) -> Any:
        if isinstance(video_source, dict):
            return video_source.get(key)
        return getattr(video_source, key, None)

    session_start_iso = _serialize_datetime(session_context.get("session_start_time"))
    session_end_iso = _serialize_datetime(session_context.get("session_end_time"))

    system_prompt = render_template("video_recognition/system_rules.j2")

    user_prompt = "\n\n".join(
        [
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
            render_template(
                "video_recognition/task.j2",
                session_context=session_context,
                session_start_time_iso=session_start_iso,
                session_end_time_iso=session_end_iso,
                strategy_context=strategy_context,
                event_type_list=event_type_list,
                note_type_list=sorted(ANALYSIS_NOTE_TYPES),
            ),
        ]
    )

    return system_prompt, user_prompt
