"""日报数据压缩逻辑。

从 daily_summary builder 中拆出的纯数据处理模块，
负责事件聚类、去重、截断等操作。
"""

from typing import Any

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
