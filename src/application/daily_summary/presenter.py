from src.models.daily_summary import DailySummary
from src.schemas.daily_summary import DailySummaryResponse


def _build_detail_text(summary: DailySummary) -> str:
    subject_sections = summary.subject_sections_json or []
    attention_items = summary.attention_items_json or []
    lines: list[str] = []

    if isinstance(subject_sections, list) and subject_sections:
        lines.append("对象小结：")
        for item in subject_sections:
            if not isinstance(item, dict):
                continue
            subject_name = str(item.get("subject_name") or "未知对象")
            subject_type = str(item.get("subject_type") or "unknown")
            subject_type_label = (
                "成员"
                if subject_type == "member"
                else "宠物"
                if subject_type == "pet"
                else subject_type
            )
            summary_text = str(item.get("summary") or "")
            if not summary_text.strip():
                continue
            lines.append(f"- {subject_name}（{subject_type_label}）：{summary_text}")

    if isinstance(attention_items, list) and attention_items:
        if lines:
            lines.append("")
        lines.append("关注事项：")
        for item in attention_items:
            if not isinstance(item, dict):
                continue
            level = str(item.get("level") or "low")
            title = str(item.get("title") or "未命名关注项")
            summary_text = str(item.get("summary") or "")
            lines.append(f"- [{level}] {title}：{summary_text}")

    return "\n".join(lines).strip()


def to_daily_summary_response(summary: DailySummary) -> DailySummaryResponse:
    return DailySummaryResponse(
        **DailySummaryResponse.model_validate(summary).model_dump(exclude={"detail_text"}),
        detail_text=_build_detail_text(summary),
    )
