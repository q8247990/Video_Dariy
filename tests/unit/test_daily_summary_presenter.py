from datetime import date, datetime

from src.application.daily_summary.presenter import to_daily_summary_response
from src.models.daily_summary import DailySummary


def test_to_daily_summary_response_builds_detail_text() -> None:
    summary = DailySummary(
        id=1,
        summary_date=date(2026, 3, 14),
        summary_title="2026-03-14 家庭日报",
        overall_summary="昨天整体平稳。",
        subject_sections_json=[
            {
                "subject_name": "爸爸",
                "subject_type": "member",
                "summary": "上午在客厅活动较多。",
                "attention_needed": False,
            }
        ],
        attention_items_json=[
            {
                "title": "门口短暂停留",
                "summary": "傍晚门口有短暂停留，建议留意。",
                "level": "low",
            }
        ],
        event_count=3,
        provider_id=None,
        generated_at=datetime(2026, 3, 15, 8, 0, 0),
    )

    response = to_daily_summary_response(summary)

    assert response.detail_text is not None
    assert "对象小结" in response.detail_text
    assert "关注事项" in response.detail_text
    assert "爸爸（成员）" in response.detail_text
