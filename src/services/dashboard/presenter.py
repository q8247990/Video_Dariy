from datetime import date, datetime
from typing import Optional

from src.core.i18n import t
from src.models.event_record import EventRecord
from src.models.video_source import VideoSource
from src.schemas.dashboard import (
    DashboardAction,
    DashboardAttentionEvent,
    DashboardEventSummary,
    DashboardFocusCount,
    DashboardLatestDailySummary,
    DashboardStatusItem,
    DashboardSystemStatus,
    DashboardTaskSummary,
)


class DashboardPresenter:
    """Pure presentation helpers that turn raw dashboard data into response schemas.

    No DB access here — callers gather raw rows via
    :mod:`src.services.dashboard.queries` and pass them in.
    """

    @staticmethod
    def system_status(onboarding_status: dict, locale: str) -> DashboardSystemStatus:
        overall_status = onboarding_status["overall_status"]
        next_action = str(onboarding_status.get("next_action") or "")

        title = t(f"dashboard.title.{overall_status}", locale)
        description = t(f"dashboard.desc.{overall_status}", locale)

        steps = onboarding_status["steps"]
        video_configured = bool(steps["video_source"]["configured"])
        video_validated = bool(steps["video_source"]["validated"])
        provider_configured = bool(steps["provider"]["configured"])
        provider_tested = bool(steps["provider"]["tested"])
        daily_summary_configured = bool(steps["daily_summary"]["configured"])
        home_profile_configured = bool(steps["home_profile"]["configured"])

        items = [
            DashboardStatusItem(
                key="video_source",
                label=t("dashboard.label.video_source", locale),
                status=_bool_pair_to_status(video_configured, video_validated),
            ),
            DashboardStatusItem(
                key="provider",
                label=t("dashboard.label.provider", locale),
                status=_bool_pair_to_status(provider_configured, provider_tested),
            ),
            DashboardStatusItem(
                key="daily_summary",
                label=t("dashboard.label.daily_summary", locale),
                status="ok" if daily_summary_configured else "not_ready",
            ),
            DashboardStatusItem(
                key="home_profile",
                label=t("dashboard.label.home_profile", locale),
                status="ok" if home_profile_configured else "partial",
            ),
        ]

        if overall_status == "basic_not_ready":
            primary_action = DashboardAction(
                label=t("dashboard.action.continue_init", locale),
                target=_onboarding_target_from_action(next_action),
            )
        elif overall_status == "basic_ready":
            primary_action = DashboardAction(
                label=t("dashboard.action.complete_profile", locale),
                target="/onboarding/personalize/profile",
            )
        else:
            primary_action = DashboardAction(
                label=t("dashboard.action.view_status", locale),
                target="/system-status",
            )

        return DashboardSystemStatus(
            overall_status=overall_status,
            title=title,
            description=description,
            items=items,
            primary_action=primary_action,
            detail_action=DashboardAction(
                label=t("dashboard.action.view_status", locale),
                target="/system-status",
            ),
        )

    @staticmethod
    def event_summary(
        attention_event_count: int,
        focus_counts: list[DashboardFocusCount],
    ) -> DashboardEventSummary:
        return DashboardEventSummary(
            attention_event_count=attention_event_count,
            focus_counts=focus_counts,
        )

    @staticmethod
    def latest_daily_summary(
        latest_summary_exist: bool,
        latest_summary_date: Optional[date],
        latest_summary_overall: Optional[str],
        latest_task_status: Optional[str],
    ) -> DashboardLatestDailySummary:
        if latest_summary_exist:
            return DashboardLatestDailySummary(
                exists=True,
                date=latest_summary_date,
                status="success",
                summary_preview=_truncate_text(latest_summary_overall, 120),
            )
        if latest_task_status == "failed":
            return DashboardLatestDailySummary(
                exists=False,
                status="failed",
                empty_reason="failed",
            )
        return DashboardLatestDailySummary(
            exists=False,
            status="empty",
            empty_reason="not_generated_yet",
        )

    @staticmethod
    def task_summary(
        last_scan_at: Optional[datetime],
        last_analysis_status: Optional[str],
        last_daily_summary_status: Optional[str],
        failed_task_count_24h: int,
    ) -> DashboardTaskSummary:
        return DashboardTaskSummary(
            last_scan_at=last_scan_at,
            last_analysis_status=last_analysis_status,
            last_daily_summary_status=last_daily_summary_status,
            failed_task_count_24h=failed_task_count_24h,
        )

    @staticmethod
    def attention_events(
        rows: list[tuple[EventRecord, VideoSource]], locale: str
    ) -> list[DashboardAttentionEvent]:
        result: list[DashboardAttentionEvent] = []
        for event, source in rows:
            display_text = event.title or event.description
            result.append(
                DashboardAttentionEvent(
                    id=event.id,
                    title=_build_event_title(display_text, locale),
                    summary=_truncate_text(display_text, 60),
                    event_time=event.event_start_time,
                    camera_name=source.camera_name,
                )
            )
        return result


def _bool_pair_to_status(configured: bool, checked: bool) -> str:
    if not configured:
        return "not_ready"
    if checked:
        return "ok"
    return "error"


def _onboarding_target_from_action(action: str) -> str:
    if action == "configure_video_source":
        return "/onboarding/basic/video"
    if action == "configure_provider":
        return "/onboarding/basic/provider"
    if action == "configure_daily_summary":
        return "/onboarding/basic/summary-time"
    if action == "configure_home_profile":
        return "/onboarding/personalize/profile"
    if action in {"configure_system_style", "configure_assistant_name"}:
        return "/onboarding/personalize/style"
    return "/onboarding"


def _truncate_text(value: str | None, limit: int) -> str:
    text = (value or "").strip()
    if len(text) <= limit:
        return text
    return f"{text[:limit].rstrip()}..."


def _build_event_title(description: str, locale: str) -> str:
    text = description.strip()
    if not text:
        return t("dashboard.event.unnamed", locale)
    if len(text) <= 18:
        return text
    return f"{text[:18].rstrip()}..."
