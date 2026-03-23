import json
from datetime import date
from typing import Any

from src.services.prompt_builder.templates.daily_summary.system_rules import (
    SYSTEM_DAILY_SUMMARY_RULES_PROMPT,
)

MAX_SUBJECT_CLUSTERS = 16
RESERVED_LONG_TAIL_CLUSTERS = 4
MAX_ATTENTION_ITEMS = 8
MAX_DATA_INPUT_PROMPT_CHARS = 12000
MAX_CLUSTER_SUMMARY_LEN = 72
MAX_ATTENTION_SUMMARY_LEN = 84

RISK_EVENT_TYPES = {
    "unknown_person_appear",
    "abnormal_stay",
    "scene_attention_needed",
}


def _normalize_text(value: str) -> str:
    text = " ".join((value or "").strip().split())
    for token in ["再次", "继续", "短暂", "短暂停留", "有", "出现", "进入画面", "画面中"]:
        text = text.replace(token, "")
    return " ".join(text.split())


def _importance_rank(value: str | None) -> int:
    level = (value or "").strip().lower()
    if level == "high":
        return 3
    if level == "medium":
        return 2
    if level == "low":
        return 1
    return 0


def _truncate_text(value: str, max_len: int) -> str:
    text = (value or "").strip()
    if len(text) <= max_len:
        return text
    return f"{text[: max_len - 1]}…"


def _compress_subject_section(section: dict[str, Any]) -> dict[str, Any]:
    event_items = section.get("related_event_summaries")
    if not isinstance(event_items, list):
        event_items = []

    cluster_map: dict[str, dict[str, Any]] = {}
    for item in event_items:
        if not isinstance(item, dict):
            continue
        event_type = str(item.get("event_type") or "unknown")
        title = str(item.get("title") or "未命名事件").strip()
        summary = str(item.get("summary") or "").strip()
        importance_level = str(item.get("importance_level") or "")
        recognition_status = str(item.get("recognition_status") or "unknown")

        dedup_key = "|".join(
            [
                event_type,
                _normalize_text(title),
                _normalize_text(summary),
            ]
        )

        cluster = cluster_map.get(dedup_key)
        if cluster is None:
            cluster_map[dedup_key] = {
                "event_type": event_type,
                "title": title,
                "summary": _truncate_text(summary, MAX_CLUSTER_SUMMARY_LEN),
                "occurrence_count": 1,
                "max_importance_rank": _importance_rank(importance_level),
                "importance_level": importance_level,
                "recognition_status": recognition_status,
            }
            continue

        cluster["occurrence_count"] += 1
        current_rank = _importance_rank(importance_level)
        if current_rank > int(cluster["max_importance_rank"]):
            cluster["max_importance_rank"] = current_rank
            cluster["importance_level"] = importance_level
        if recognition_status == "confirmed":
            cluster["recognition_status"] = "confirmed"

    clusters = list(cluster_map.values())

    def _cluster_rank(item: dict[str, Any]) -> tuple[int, int, int, int]:
        return (
            int(item.get("max_importance_rank") or 0),
            1 if item.get("event_type") in RISK_EVENT_TYPES else 0,
            int(item.get("occurrence_count") or 0),
            1 if item.get("recognition_status") == "confirmed" else 0,
        )

    clusters.sort(key=_cluster_rank, reverse=True)

    keep_total = MAX_SUBJECT_CLUSTERS
    reserved = min(RESERVED_LONG_TAIL_CLUSTERS, keep_total)
    primary_keep = max(keep_total - reserved, 0)
    selected = clusters[:primary_keep]
    selected_types = {str(item.get("event_type") or "") for item in selected}

    long_tail_candidates = clusters[primary_keep:]
    long_tail_candidates.sort(
        key=lambda item: (
            1 if item.get("event_type") in RISK_EVENT_TYPES else 0,
            1 if str(item.get("event_type") or "") not in selected_types else 0,
            int(item.get("max_importance_rank") or 0),
            int(item.get("occurrence_count") or 0),
        ),
        reverse=True,
    )
    selected.extend(long_tail_candidates[:reserved])

    selected.sort(key=_cluster_rank, reverse=True)
    selected = selected[:keep_total]

    return {
        "subject_name": str(section.get("subject_name") or "未知对象"),
        "subject_type": str(section.get("subject_type") or "unknown"),
        "raw_event_count": int(section.get("related_event_count") or len(event_items)),
        "clusters": selected,
    }


def _compress_attention_candidates(
    attention_candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    cluster_map: dict[str, dict[str, Any]] = {}
    for item in attention_candidates:
        if not isinstance(item, dict):
            continue
        event_type = str(item.get("event_type") or "unknown")
        title = str(item.get("title") or "未命名关注项").strip()
        summary = str(item.get("summary") or "").strip()
        importance_level = str(item.get("importance_level") or "")
        dedup_key = "|".join([event_type, _normalize_text(title), _normalize_text(summary)])

        cluster = cluster_map.get(dedup_key)
        if cluster is None:
            cluster_map[dedup_key] = {
                "event_type": event_type,
                "title": title,
                "summary": _truncate_text(summary, MAX_ATTENTION_SUMMARY_LEN),
                "occurrence_count": 1,
                "max_importance_rank": _importance_rank(importance_level),
                "importance_level": importance_level,
            }
            continue

        cluster["occurrence_count"] += 1
        current_rank = _importance_rank(importance_level)
        if current_rank > int(cluster["max_importance_rank"]):
            cluster["max_importance_rank"] = current_rank
            cluster["importance_level"] = importance_level

    rows = list(cluster_map.values())
    rows.sort(
        key=lambda item: (
            int(item.get("max_importance_rank") or 0),
            1 if item.get("event_type") in RISK_EVENT_TYPES else 0,
            int(item.get("occurrence_count") or 0),
        ),
        reverse=True,
    )
    return rows[:MAX_ATTENTION_ITEMS]


def _render_compact_data_prompt(
    *,
    compressed_subject_sections: list[dict[str, Any]],
    missing_subjects: list[str],
    compressed_attention_candidates: list[dict[str, Any]],
) -> str:
    lines: list[str] = []
    lines.append("数据输入（紧凑版）：")
    lines.append("对象聚合摘要：")
    if not compressed_subject_sections:
        lines.append("- 无对象事件摘要")
    for section in compressed_subject_sections:
        subject_name = section["subject_name"]
        subject_type = section["subject_type"]
        raw_event_count = section["raw_event_count"]
        clusters = section["clusters"]
        lines.append(
            "- 对象="
            + f"{subject_name}({subject_type})"
            + f" | 原始命中={raw_event_count}"
            + f" | 聚合簇={len(clusters)}"
        )
        for index, cluster in enumerate(clusters, start=1):
            lines.append(
                "  "
                + f"{index:02d}. [{cluster['importance_level'] or 'unknown'}]"
                + f" 类型={cluster['event_type']}"
                + f" 次数={cluster['occurrence_count']}"
                + f" 识别={cluster['recognition_status']}"
                + f" 标题={cluster['title']}"
                + f" 摘要={cluster['summary']}"
            )

    missing_line = "、".join(missing_subjects) if missing_subjects else "无"
    lines.append(f"未命中对象：{missing_line}")

    lines.append("关注候选摘要：")
    if not compressed_attention_candidates:
        lines.append("- 无关注候选")
    for index, item in enumerate(compressed_attention_candidates, start=1):
        lines.append(
            f"- {index:02d}. [{item['importance_level'] or 'unknown'}]"
            + f" 类型={item['event_type']}"
            + f" 次数={item['occurrence_count']}"
            + f" 标题={item['title']}"
            + f" 摘要={item['summary']}"
        )

    text = "\n".join(lines)
    if len(text) <= MAX_DATA_INPUT_PROMPT_CHARS:
        return text
    return f"{text[: MAX_DATA_INPUT_PROMPT_CHARS - 1]}…"


def compress_daily_input(
    *,
    subject_sections: list[dict[str, Any]],
    missing_subjects: list[str],
    attention_candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    compressed_subject_sections = [
        _compress_subject_section(section)
        for section in subject_sections
        if isinstance(section, dict)
    ]
    compressed_attention_candidates = _compress_attention_candidates(attention_candidates)
    return {
        "subject_sections": compressed_subject_sections,
        "missing_subjects": missing_subjects,
        "attention_candidates": compressed_attention_candidates,
    }


def build_subject_summary_prompt(
    *,
    home_context: dict[str, Any],
    summary_date: date,
    time_range_start: str,
    time_range_end: str,
    subject_section: dict[str, Any],
) -> str:
    home_prompt = build_daily_home_context_prompt(home_context)
    subject_name = str(subject_section.get("subject_name") or "未知对象")
    subject_type = str(subject_section.get("subject_type") or "unknown")
    raw_event_count = int(subject_section.get("raw_event_count") or 0)
    raw_clusters = subject_section.get("clusters")
    clusters: list[dict[str, Any]] = []
    if isinstance(raw_clusters, list):
        clusters = [item for item in raw_clusters if isinstance(item, dict)]

    lines: list[str] = []
    lines.append("对象摘要任务：")
    lines.append(
        f"日期={summary_date} 时间范围={time_range_start}~{time_range_end}"
        + f" 对象={subject_name}({subject_type})"
    )
    lines.append(f"原始命中事件数={raw_event_count}，聚合簇数={len(clusters)}")
    lines.append("对象事件簇：")
    for index, cluster in enumerate(clusters, start=1):
        lines.append(
            f"- {index:02d}. [{cluster.get('importance_level') or 'unknown'}]"
            + f" 类型={cluster.get('event_type')}"
            + f" 次数={cluster.get('occurrence_count')}"
            + f" 识别={cluster.get('recognition_status')}"
            + f" 标题={cluster.get('title')}"
            + f" 摘要={cluster.get('summary')}"
        )

    lines.append(
        "写作要求："
        + "1) 输出 2~4 句自然中文，约 50~90 字；"
        + "2) 先结论再补充关键活动；"
        + "3) 禁止省略号和半句。"
    )
    lines.append(
        "请仅输出 JSON："
        + '{"summary":"string","attention_needed":true/false}'
        + "，不要输出其他内容。"
    )

    return "\n\n".join(
        [
            f"系统总结规则：\n{SYSTEM_DAILY_SUMMARY_RULES_PROMPT}",
            home_prompt,
            "\n".join(lines),
        ]
    )


def build_daily_rollup_prompt(
    *,
    home_context: dict[str, Any],
    summary_date: date,
    subject_results: list[dict[str, Any]],
    attention_candidates: list[dict[str, Any]],
) -> str:
    home_prompt = build_daily_home_context_prompt(home_context)
    lines: list[str] = []
    lines.append("汇总任务：")
    lines.append(f"日期={summary_date}，基于对象摘要生成日报总览与关注事项。")
    lines.append("对象摘要：")
    if not subject_results:
        lines.append("- 无对象摘要")
    for index, row in enumerate(subject_results, start=1):
        lines.append(
            f"- {index:02d}. 对象={row.get('subject_name')}({row.get('subject_type')})"
            + f" 活动度={row.get('activity_score', 0)}"
            + f" 关注={row.get('attention_needed')}"
            + f" 摘要={row.get('summary')}"
        )

    lines.append("关注候选：")
    if not attention_candidates:
        lines.append("- 无关注候选")
    for index, item in enumerate(attention_candidates, start=1):
        lines.append(
            f"- {index:02d}. [{item.get('importance_level') or 'unknown'}]"
            + f" 类型={item.get('event_type')}"
            + f" 次数={item.get('occurrence_count')}"
            + f" 标题={item.get('title')}"
            + f" 摘要={item.get('summary')}"
        )

    lines.append(
        "写作要求："
        + "1) 必须保留所有对象，且按活动度从高到低排序；"
        + "2) overall_summary 120~180 字；"
        + "3) 每个对象摘要 50~90 字；"
        + "4) 全文控制在 500~900 字；"
        + "5) 禁止省略号和半句。"
    )

    lines.append(
        "请仅输出 JSON："
        + '{"overall_summary":"...","attention_items":['
        + '{"title":"...","summary":"...","level":"low|medium|high"}]}'
        + "，不要输出其他内容。"
    )

    return "\n\n".join(
        [
            f"系统总结规则：\n{SYSTEM_DAILY_SUMMARY_RULES_PROMPT}",
            home_prompt,
            "\n".join(lines),
        ]
    )


def build_daily_home_context_prompt(home_context: dict[str, Any]) -> str:
    home_profile = home_context.get("home_profile", {})
    members = home_context.get("members", [])
    pets = home_context.get("pets", [])

    payload = {
        "home_profile": {
            "home_name": home_profile.get("home_name"),
            "family_tags": home_profile.get("family_tags", []),
            "focus_points": home_profile.get("focus_points", []),
            "system_style": home_profile.get("system_style"),
            "style_preference_text": home_profile.get("style_preference_text"),
            "assistant_name": home_profile.get("assistant_name"),
            "home_note": home_profile.get("home_note"),
        },
        "known_subjects": {
            "members": [
                {
                    "name": item.get("name"),
                    "role_type": item.get("role_type"),
                    "age_group": item.get("age_group"),
                }
                for item in members
            ],
            "pets": [
                {
                    "name": item.get("name"),
                    "role_type": item.get("role_type"),
                    "breed": item.get("breed"),
                }
                for item in pets
            ],
        },
    }
    return f"家庭上下文：\n{json.dumps(payload, ensure_ascii=False, indent=2)}"


def build_daily_task_prompt(
    *,
    summary_date: date,
    time_range_start: str,
    time_range_end: str,
) -> str:
    payload = {
        "summary_date": str(summary_date),
        "summary_scope": "yesterday",
        "time_range": {
            "start": time_range_start,
            "end": time_range_end,
        },
        "task_goal": "生成给家庭用户阅读的结构化日报",
        "output_schema": {
            "overall_summary": "string",
            "subject_sections": [
                {
                    "subject_name": "string",
                    "subject_type": "member|pet",
                    "summary": "string",
                    "attention_needed": True,
                }
            ],
            "attention_items": [
                {
                    "title": "string",
                    "summary": "string",
                    "level": "low|medium|high",
                }
            ],
        },
        "constraints": [
            "subject_sections 仅包含有明确内容的已知对象",
            "attention_items 最多 3 条，可为空",
            "总字数尽量控制在 900 字以内",
            "仅输出 JSON 对象",
        ],
    }
    return f"任务上下文：\n{json.dumps(payload, ensure_ascii=False, indent=2)}"


def build_daily_data_input_prompt(
    *,
    subject_sections: list[dict[str, Any]],
    missing_subjects: list[str],
    attention_candidates: list[dict[str, Any]],
) -> str:
    compressed = compress_daily_input(
        subject_sections=subject_sections,
        missing_subjects=missing_subjects,
        attention_candidates=attention_candidates,
    )
    return _render_compact_data_prompt(
        compressed_subject_sections=compressed["subject_sections"],
        missing_subjects=compressed["missing_subjects"],
        compressed_attention_candidates=compressed["attention_candidates"],
    )


def build_daily_summary_prompt(input_data: dict[str, Any]) -> str:
    home_context = input_data["home_context"]
    summary_date = input_data["summary_date"]
    time_range = input_data["time_range"]
    subject_sections = input_data["subject_sections"]
    missing_subjects = input_data["missing_subjects"]
    attention_candidates = input_data["attention_candidates"]

    task_prompt = build_daily_task_prompt(
        summary_date=summary_date,
        time_range_start=time_range["start"],
        time_range_end=time_range["end"],
    )
    data_prompt = build_daily_data_input_prompt(
        subject_sections=subject_sections,
        missing_subjects=missing_subjects,
        attention_candidates=attention_candidates,
    )

    return "\n\n".join(
        [
            f"系统总结规则：\n{SYSTEM_DAILY_SUMMARY_RULES_PROMPT}",
            build_daily_home_context_prompt(home_context),
            task_prompt,
            data_prompt,
        ]
    )
