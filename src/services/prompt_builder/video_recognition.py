import json
from datetime import datetime
from typing import Any

from src.services.prompt_builder.templates.video_recognition.system_rules import (
    SYSTEM_RECOGNITION_RULES_PROMPT,
)
from src.services.video_analysis.enums import ANALYSIS_NOTE_TYPES, VIDEO_EVENT_TYPES


def _compact_list(value: list[Any] | None) -> str:
    if not value:
        return "无"
    return "、".join(str(item) for item in value if str(item).strip()) or "无"


def _compact_text(value: Any) -> str:
    if value is None:
        return "无"
    text = str(value).strip()
    return text or "无"


def build_home_context_prompt(home_context: dict[str, Any]) -> str:
    home_profile = home_context.get("home_profile", {})
    members = home_context.get("members", [])
    pets = home_context.get("pets", [])

    member_texts: list[str] = []
    for item in members:
        member_texts.append(
            "{name}(角色:{role}, 年龄段:{age}, 外观:{appearance}, 备注:{note})".format(
                name=_compact_text(item.get("name")),
                role=_compact_text(item.get("role_type")),
                age=_compact_text(item.get("age_group")),
                appearance=_compact_text(item.get("appearance_desc")),
                note=_compact_text(item.get("note")),
            )
        )

    pet_texts: list[str] = []
    for item in pets:
        pet_texts.append(
            "{name}(类型:{role}, 品种:{breed}, 外观:{appearance}, 备注:{note})".format(
                name=_compact_text(item.get("name")),
                role=_compact_text(item.get("role_type")),
                breed=_compact_text(item.get("breed")),
                appearance=_compact_text(item.get("appearance_desc")),
                note=_compact_text(item.get("note")),
            )
        )

    lines = [
        "家庭上下文：",
        (
            "家庭概况: 家庭名={home_name}; 标签={family_tags}; 关注点={focus_points}; "
            "风格={system_style}; 风格偏好={style}; 助手名={assistant}; 备注={note}."
        ).format(
            home_name=_compact_text(home_profile.get("home_name")),
            family_tags=_compact_list(home_profile.get("family_tags", [])),
            focus_points=_compact_list(home_profile.get("focus_points", [])),
            system_style=_compact_text(home_profile.get("system_style")),
            style=_compact_text(home_profile.get("style_preference_text")),
            assistant=_compact_text(home_profile.get("assistant_name")),
            note=_compact_text(home_profile.get("home_note")),
        ),
        f"家庭成员: {'；'.join(member_texts) if member_texts else '无' }。",
        f"宠物信息: {'；'.join(pet_texts) if pet_texts else '无' }。",
        "边界: 家庭档案仅作为辅助上下文，不能替代视频证据。",
    ]
    return "\n".join(lines)


def build_camera_context_prompt(video_source: Any) -> str:
    def _read_value(key: str) -> Any:
        if isinstance(video_source, dict):
            return video_source.get(key)
        return getattr(video_source, key, None)

    payload = {
        "source_name": _read_value("source_name"),
        "camera_name": _read_value("camera_name"),
        "location_name": _read_value("location_name"),
        "camera_note": _read_value("prompt_text"),
        "interference_hint": "注意电视反光、玻璃反射、走廊远处小目标等误判风险。",
    }
    return f"摄像头上下文：\n{json.dumps(payload, ensure_ascii=False, indent=2)}"


def build_strategy_note(ingest_type: str, source_type: str) -> str:
    if ingest_type == "xiaomi_nas_backup":
        return (
            "当前 session 来源于变化触发型存储片段，通常具备分析价值；"
            "请优先做语义归纳，不要因短暂静止忽略整段价值。"
        )
    if source_type == "stream":
        return "当前场景接近流式接入，请提高事件抽取阈值，避免噪声事件。"
    return "按常规家庭监控分析策略执行，保持中等事件粒度。"


def _serialize_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()


def build_task_prompt(task_context: dict[str, Any]) -> str:
    session_context = task_context.get("session_context", {})
    strategy_context = task_context.get("strategy_context", {})
    event_type_list = task_context.get("event_type_list") or sorted(VIDEO_EVENT_TYPES)

    schema_hint = {
        "session_summary": {
            "summary_text": "string",
            "activity_level": "low|medium|high",
            "main_subjects": ["string"],
            "has_important_event": True,
        },
        "events": [
            {
                "offset_start_sec": 0,
                "offset_end_sec": 12,
                "event_type": "member_appear",
                "title": "string",
                "summary": "string",
                "detail": "string",
                "related_entities": [
                    {
                        "entity_type": "member|pet|unknown_person|other",
                        "display_name": "string",
                        "matched_profile_name": "string|null",
                        "recognition_status": "confirmed|suspected|unknown",
                        "confidence": 0.9,
                    }
                ],
                "observed_actions": ["string"],
                "interpreted_state": ["string"],
                "confidence": 0.9,
                "importance_level": "low|medium|high",
            }
        ],
        "analysis_notes": [{"type": "identity_uncertain", "note": "string"}],
    }

    lines = [
        "任务级 Prompt：",
        "任务目标: 对当前 session 做结构化识别，输出可入库事件。",
        (
            "会话上下文: session_id={session_id}; source_id={source_id}; start={start}; end={end}; "
            "duration={duration}; segment_index={segment_index}; "
            "segment_start_offset_sec={segment_start}; segment_duration_seconds={segment_duration}."
        ).format(
            session_id=session_context.get("session_id"),
            source_id=session_context.get("source_id"),
            start=_serialize_datetime(session_context.get("session_start_time")),
            end=_serialize_datetime(session_context.get("session_end_time")),
            duration=session_context.get("total_duration_seconds"),
            segment_index=session_context.get("segment_index"),
            segment_start=session_context.get("segment_start_offset_sec"),
            segment_duration=session_context.get("segment_duration_seconds"),
        ),
        (
            "策略上下文: ingest_type={ingest_type}; source_type={source_type}; "
            "strategy_note={strategy_note}"
        ).format(
            ingest_type=_compact_text(strategy_context.get("ingest_type")),
            source_type=_compact_text(strategy_context.get("source_type")),
            strategy_note=_compact_text(strategy_context.get("strategy_note")),
        ),
        "事件抽取规则: event_type 枚举={event_types}; analysis_note_type 枚举={note_types}; "
        "merge_similar_actions=true; split_only_on_significant_change=true.".format(
            event_types=_compact_list(event_type_list),
            note_types=_compact_list(sorted(ANALYSIS_NOTE_TYPES)),
        ),
        "输出 schema（完整示例，必须严格遵守）:",
        json.dumps(schema_hint, ensure_ascii=False, indent=2),
        "最终约束: 只能输出 JSON 对象; 所有 offset 必须为非负数且 end>=start; "
        "offset 以当前输入视频片段起点为 0 秒; event_type 必须来自枚举; "
        "importance_level 必须来自 low/medium/high; detail 需详细描述可观察事实; "
        "当识别 confirmed 且可匹配家庭档案时，优先使用档案姓名（成员与宠物都适用）。",
    ]
    return "\n".join(lines)


def build_video_recognition_prompt(input_data: dict[str, Any]) -> str:
    home_context = input_data["home_context"]
    video_source = input_data["video_source"]
    session_context = input_data["session_context"]
    strategy_context = input_data["strategy_context"]
    event_type_list = input_data.get("event_type_list") or sorted(VIDEO_EVENT_TYPES)

    task_prompt = build_task_prompt(
        {
            "session_context": session_context,
            "strategy_context": strategy_context,
            "event_type_list": event_type_list,
        }
    )

    return "\n\n".join(
        [
            f"系统识别规则：\n{SYSTEM_RECOGNITION_RULES_PROMPT}",
            build_home_context_prompt(home_context),
            build_camera_context_prompt(video_source),
            task_prompt,
        ]
    )
