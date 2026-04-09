from src.core.i18n.locale_directive import get_presenter_label
from src.models.daily_summary import DailySummary
from src.schemas.daily_summary import DailySummaryResponse


def _subject_type_label(subject_type: str, locale: str | None) -> str:
    if subject_type == "member":
        return get_presenter_label("subject_type_member", locale)
    if subject_type == "pet":
        return get_presenter_label("subject_type_pet", locale)
    return subject_type


def _build_detail_text(summary: DailySummary, locale: str | None = None) -> str:
    subject_sections = summary.subject_sections_json or []
    attention_items = summary.attention_items_json or []
    lines: list[str] = []

    if isinstance(subject_sections, list) and subject_sections:
        lines.append(get_presenter_label("subject_sections_header", locale))
        for item in subject_sections:
            if not isinstance(item, dict):
                continue
            subject_name = str(
                item.get("subject_name") or get_presenter_label("unnamed_subject", locale)
            )
            subject_type = str(item.get("subject_type") or "unknown")
            subject_type_label = _subject_type_label(subject_type, locale)
            summary_text = str(item.get("summary") or "")
            if not summary_text.strip():
                continue
            lines.append(f"- {subject_name}（{subject_type_label}）：{summary_text}")

    if isinstance(attention_items, list) and attention_items:
        if lines:
            lines.append("")
        lines.append(get_presenter_label("attention_items_header", locale))
        for item in attention_items:
            if not isinstance(item, dict):
                continue
            level = str(item.get("level") or "low")
            title = str(item.get("title") or get_presenter_label("unnamed_attention", locale))
            summary_text = str(item.get("summary") or "")
            lines.append(f"- [{level}] {title}：{summary_text}")

    return "\n".join(lines).strip()


def to_daily_summary_response(
    summary: DailySummary, locale: str | None = None
) -> DailySummaryResponse:
    return DailySummaryResponse(
        **DailySummaryResponse.model_validate(summary).model_dump(exclude={"detail_text"}),
        detail_text=_build_detail_text(summary, locale),
    )
