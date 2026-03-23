from typing import Any

from src.application.qa.schemas import (
    CompressedEvidence,
    DailySummaryEvidence,
    EventEvidence,
    QueryPlan,
    SessionEvidence,
)

MAX_DETAIL_LENGTH = 150
MAX_SUMMARY_LENGTH = 80
MAX_ATTENTION_SUMMARY_LENGTH = 60


# ---------------------------------------------------------------------------
# 家庭上下文压缩
# ---------------------------------------------------------------------------


def compress_home_context(home_context: dict[str, Any]) -> str:
    home_profile = home_context.get("home_profile", {})
    members = home_context.get("members", [])
    pets = home_context.get("pets", [])

    member_parts = [f"{item.get('name', '?')}({item.get('role_type', '?')})" for item in members]
    pet_parts = [f"{item.get('name', '?')}({item.get('role_type', '?')})" for item in pets]

    lines = [
        f"家庭: {home_profile.get('home_name', '')}",
        f"标签: {'、'.join(home_profile.get('family_tags', [])) or '无'}",
        f"关注: {'、'.join(home_profile.get('focus_points', [])) or '无'}",
        f"成员: {'、'.join(member_parts) if member_parts else '无'}",
        f"宠物: {'、'.join(pet_parts) if pet_parts else '无'}",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# QueryPlan 压缩（用于回答 prompt）
# ---------------------------------------------------------------------------


def compress_query_plan(query_plan: QueryPlan) -> str:
    parts = [f"模式={query_plan.question_mode}"]
    if query_plan.time_range:
        start = query_plan.time_range.start.strftime("%m-%d %H:%M")
        end = query_plan.time_range.end.strftime("%m-%d %H:%M")
        parts.append(f"时间={start}~{end}")
    if query_plan.subjects:
        parts.append(f"主体={'、'.join(query_plan.subjects)}")
    if query_plan.event_types:
        parts.append(f"类型={'、'.join(query_plan.event_types)}")
    if query_plan.importance_levels:
        parts.append(f"重要性={'、'.join(query_plan.importance_levels)}")
    return " | ".join(parts)


# ---------------------------------------------------------------------------
# 日报压缩
# ---------------------------------------------------------------------------


def _truncate(text: str, max_len: int) -> str:
    text = text.strip()
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"


def compress_daily_summaries(summaries: list[DailySummaryEvidence]) -> str:
    if not summaries:
        return ""

    lines: list[str] = []
    for s in summaries:
        overall = _truncate(s.overall_summary, 200)
        line = f"D {s.summary_date} | event_count={s.event_count} | overall={overall}"

        # 关注事项
        attention_parts: list[str] = []
        for item in s.attention_items[:3]:
            title = item.get("title", "")
            summary = _truncate(item.get("summary", ""), MAX_ATTENTION_SUMMARY_LENGTH)
            level = item.get("level", "")
            if title:
                attention_parts.append(f"[{level}]{title}: {summary}")
        if attention_parts:
            line += " | attention=" + "; ".join(attention_parts)

        # 主体小结
        subject_parts: list[str] = []
        for item in s.subject_sections[:4]:
            name = item.get("subject_name", "")
            summary = _truncate(item.get("summary", ""), MAX_SUMMARY_LENGTH)
            if name and summary:
                subject_parts.append(f"{name}: {summary}")
        if subject_parts:
            line += " | subjects=" + "; ".join(subject_parts)

        lines.append(line)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Session 压缩
# ---------------------------------------------------------------------------


def compress_sessions(sessions: list[SessionEvidence]) -> str:
    if not sessions:
        return ""

    lines: list[str] = []
    for s in sessions:
        start = s.session_start_time.strftime("%m-%d %H:%M")
        end = s.session_end_time.strftime("%H:%M")
        subjects = "、".join(s.main_subjects) if s.main_subjects else "无"
        important = "yes" if s.has_important_event else "no"
        summary = _truncate(s.summary_text, MAX_SUMMARY_LENGTH)

        line = (
            f"S{s.id} | {start}~{end} | activity={s.activity_level}"
            f" | important={important} | subjects={subjects}"
            f" | summary={summary}"
        )
        lines.append(line)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Event 压缩
# ---------------------------------------------------------------------------


def _extract_subject_names(related_entities: list[dict[str, Any]]) -> str:
    names: list[str] = []
    for entity in related_entities:
        status = entity.get("recognition_status", "")
        if status not in ("confirmed", "suspected"):
            continue
        name = entity.get("matched_profile_name") or entity.get("display_name") or ""
        if name and name not in names:
            names.append(name)
    return "、".join(names) if names else "无"


def compress_events(events: list[EventEvidence]) -> str:
    if not events:
        return ""

    lines: list[str] = []
    for e in events:
        time_str = e.event_start_time.strftime("%m-%d %H:%M")
        subject = _extract_subject_names(e.related_entities)
        summary = _truncate(e.summary, MAX_SUMMARY_LENGTH)
        detail = _truncate(e.detail, MAX_DETAIL_LENGTH)

        line = (
            f"E{e.id} | {time_str} | {e.importance_level} | {e.event_type}"
            f" | subject={subject} | summary={summary}"
        )
        if detail and detail != summary:
            line += f" | detail={detail}"
        lines.append(line)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 总入口
# ---------------------------------------------------------------------------


def compress_evidence(
    home_context: dict[str, Any],
    query_plan: QueryPlan,
    daily_summaries: list[DailySummaryEvidence],
    sessions: list[SessionEvidence],
    events: list[EventEvidence],
) -> CompressedEvidence:
    return CompressedEvidence(
        home_context_text=compress_home_context(home_context),
        query_plan_text=compress_query_plan(query_plan),
        daily_summary_text=compress_daily_summaries(daily_summaries),
        session_text=compress_sessions(sessions),
        event_text=compress_events(events),
    )
