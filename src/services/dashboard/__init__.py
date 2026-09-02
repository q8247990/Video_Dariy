from typing import Optional

from sqlalchemy.orm import Session

from src.core.i18n import DEFAULT_LOCALE, t
from src.schemas.dashboard import (
    DashboardAction,
    DashboardAlert,
    DashboardEventSummary,
    DashboardImportantEvent,
    DashboardLatestDailySummary,
    DashboardOverviewResponse,
    DashboardTaskSummary,
)
from src.services.onboarding import DEFAULT_ASSISTANT_NAME, get_onboarding_status
from src.services.pipeline_constants import TaskStatus, TaskType

from . import queries
from .presenter import DashboardPresenter


def get_dashboard_overview(
    db: Session,
    locale: Optional[str] = None,
) -> DashboardOverviewResponse:
    loc = locale or DEFAULT_LOCALE
    onboarding_status = get_onboarding_status(db)
    assistant_name = _get_assistant_name(db)

    return DashboardOverviewResponse(
        assistant_name=assistant_name,
        system_status=DashboardPresenter.system_status(onboarding_status, loc),
        alert=_build_alert(db, onboarding_status, loc),
        task_summary=_build_task_summary(db),
        event_summary=_build_event_summary(db),
        latest_daily_summary=_build_latest_daily_summary(db),
        important_events=_build_important_events(db, loc),
    )


def _get_assistant_name(db: Session) -> str:
    name = queries.assistant_name(db)
    return name or DEFAULT_ASSISTANT_NAME


def _build_alert(db: Session, onboarding_status: dict, locale: str) -> DashboardAlert:
    if onboarding_status["overall_status"] == "basic_not_ready":
        return DashboardAlert(
            show=True,
            type="basic_not_ready",
            title=t("dashboard.alert.basic_not_ready.title", locale),
            description=t("dashboard.alert.basic_not_ready.desc", locale),
            action=DashboardAction(
                label=t("dashboard.action.continue_init", locale),
                target="/onboarding",
            ),
        )

    steps = onboarding_status["steps"]
    if not steps["provider"]["configured"] or not steps["provider"]["tested"]:
        return DashboardAlert(
            show=True,
            type="provider_error",
            title=t("dashboard.alert.provider_error.title", locale),
            description=t("dashboard.alert.provider_error.desc", locale),
            action=DashboardAction(
                label=t("dashboard.alert.provider_error.action", locale),
                target="/providers",
            ),
        )

    if not steps["video_source"]["configured"] or not steps["video_source"]["validated"]:
        return DashboardAlert(
            show=True,
            type="video_source_error",
            title=t("dashboard.alert.video_source_error.title", locale),
            description=t("dashboard.alert.video_source_error.desc", locale),
            action=DashboardAction(
                label=t("dashboard.alert.video_source_error.action", locale),
                target="/video-sources",
            ),
        )

    latest_daily_task = queries.latest_task_by_type(db, TaskType.DAILY_SUMMARY_GENERATION)
    if latest_daily_task and latest_daily_task.status == TaskStatus.FAILED:
        return DashboardAlert(
            show=True,
            type="daily_summary_error",
            title=t("dashboard.alert.daily_summary_error.title", locale),
            description=t("dashboard.alert.daily_summary_error.desc", locale),
            action=DashboardAction(
                label=t("dashboard.alert.daily_summary_error.action", locale),
                target="/tasks",
            ),
        )

    failed_analysis_count = queries.failed_analysis_count_24h(db)
    if failed_analysis_count > 0:
        return DashboardAlert(
            show=True,
            type="analysis_task_error",
            title=t("dashboard.alert.analysis_task_error.title", locale),
            description=t(
                "dashboard.alert.analysis_task_error.desc",
                locale,
                count=failed_analysis_count,
            ),
            action=DashboardAction(
                label=t("dashboard.alert.analysis_task_error.action", locale),
                target="/tasks?status=failed&task_type=session_analysis",
            ),
        )

    return DashboardAlert(show=False)


def _build_task_summary(db: Session) -> DashboardTaskSummary:
    latest_analysis_task = queries.latest_task_by_type(db, TaskType.SESSION_ANALYSIS)
    latest_daily_task = queries.latest_task_by_type(db, TaskType.DAILY_SUMMARY_GENERATION)
    return DashboardPresenter.task_summary(
        last_scan_at=queries.last_scan_at(db),
        last_analysis_status=latest_analysis_task.status if latest_analysis_task else None,
        last_daily_summary_status=latest_daily_task.status if latest_daily_task else None,
        failed_task_count_24h=queries.failed_task_count_24h(db),
    )


def _build_event_summary(db: Session) -> DashboardEventSummary:
    today_count, yesterday_count, important_count = queries.event_summary_counts(db)
    return DashboardPresenter.event_summary(today_count, yesterday_count, important_count)


def _build_latest_daily_summary(db: Session) -> DashboardLatestDailySummary:
    latest = queries.latest_daily_summary(db)
    latest_task = queries.latest_task_by_type(db, TaskType.DAILY_SUMMARY_GENERATION)
    task_status = latest_task.status if latest_task else None
    return DashboardPresenter.latest_daily_summary(
        latest_summary_exist=latest is not None,
        latest_summary_date=latest.summary_date if latest else None,
        latest_summary_overall=latest.overall_summary if latest else None,
        latest_task_status=task_status,
    )


def _build_important_events(db: Session, locale: str) -> list[DashboardImportantEvent]:
    return DashboardPresenter.important_events(queries.important_event_rows(db), locale)
