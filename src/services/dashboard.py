from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import case, func
from sqlalchemy.orm import Session

from src.core.i18n import DEFAULT_LOCALE, t
from src.models.daily_summary import DailySummary
from src.models.event_record import EventRecord
from src.models.home_profile import HomeProfile
from src.models.task_log import TaskLog
from src.models.video_source import VideoSource
from src.schemas.dashboard import (
    DashboardAction,
    DashboardAlert,
    DashboardEventSummary,
    DashboardImportantEvent,
    DashboardLatestDailySummary,
    DashboardOverviewResponse,
    DashboardStatusItem,
    DashboardSystemStatus,
    DashboardTaskSummary,
)
from src.services.onboarding import DEFAULT_ASSISTANT_NAME, get_onboarding_status
from src.services.pipeline_constants import TaskStatus, TaskType

IMPORTANT_LEVELS = ("high",)


def get_dashboard_overview(
    db: Session,
    locale: Optional[str] = None,
) -> DashboardOverviewResponse:
    loc = locale or DEFAULT_LOCALE
    onboarding_status = get_onboarding_status(db)
    assistant_name = _get_assistant_name(db)

    return DashboardOverviewResponse(
        assistant_name=assistant_name,
        system_status=_build_system_status(onboarding_status, loc),
        alert=_build_alert(db, onboarding_status, loc),
        task_summary=_build_task_summary(db),
        event_summary=_build_event_summary(db),
        latest_daily_summary=_build_latest_daily_summary(db),
        important_events=_build_important_events(db, loc),
    )


def _get_assistant_name(db: Session) -> str:
    profile = db.query(HomeProfile).order_by(HomeProfile.id.asc()).first()
    if profile and (profile.assistant_name or "").strip():
        return profile.assistant_name.strip()
    return DEFAULT_ASSISTANT_NAME


def _build_system_status(onboarding_status: dict, locale: str) -> DashboardSystemStatus:
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

    latest_daily_task = _latest_task_by_type(db, TaskType.DAILY_SUMMARY_GENERATION)
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

    failed_analysis_count = (
        db.query(func.count(TaskLog.id))
        .filter(
            TaskLog.task_type == TaskType.SESSION_ANALYSIS,
            TaskLog.status == TaskStatus.FAILED,
            TaskLog.created_at >= datetime.utcnow() - timedelta(hours=24),
        )
        .scalar()
        or 0
    )
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
    last_scan_at = db.query(func.max(VideoSource.last_scan_at)).scalar()

    latest_analysis_task = _latest_task_by_type(db, TaskType.SESSION_ANALYSIS)
    latest_daily_task = _latest_task_by_type(db, TaskType.DAILY_SUMMARY_GENERATION)

    failed_task_count_24h = (
        db.query(func.count(TaskLog.id))
        .filter(
            TaskLog.status == TaskStatus.FAILED,
            TaskLog.created_at >= datetime.utcnow() - timedelta(hours=24),
        )
        .scalar()
        or 0
    )

    return DashboardTaskSummary(
        last_scan_at=last_scan_at,
        last_analysis_status=latest_analysis_task.status if latest_analysis_task else None,
        last_daily_summary_status=latest_daily_task.status if latest_daily_task else None,
        failed_task_count_24h=failed_task_count_24h,
    )


def _build_event_summary(db: Session) -> DashboardEventSummary:
    now = datetime.utcnow()
    today_start = datetime.combine(now.date(), datetime.min.time())
    yesterday_start = today_start - timedelta(days=1)

    today_event_count = (
        db.query(func.count(EventRecord.id))
        .filter(EventRecord.event_start_time >= today_start, EventRecord.event_start_time <= now)
        .scalar()
        or 0
    )

    yesterday_event_count = (
        db.query(func.count(EventRecord.id))
        .filter(
            EventRecord.event_start_time >= yesterday_start,
            EventRecord.event_start_time < today_start,
        )
        .scalar()
        or 0
    )

    important_event_count_24h = (
        db.query(func.count(EventRecord.id))
        .filter(
            EventRecord.event_start_time >= now - timedelta(hours=24),
            _important_condition(),
        )
        .scalar()
        or 0
    )

    return DashboardEventSummary(
        today_event_count=today_event_count,
        yesterday_event_count=yesterday_event_count,
        important_event_count_24h=important_event_count_24h,
    )


def _build_latest_daily_summary(db: Session) -> DashboardLatestDailySummary:
    latest = db.query(DailySummary).order_by(DailySummary.summary_date.desc()).first()
    if latest:
        return DashboardLatestDailySummary(
            exists=True,
            date=latest.summary_date,
            status="success",
            summary_preview=_truncate_text(latest.overall_summary, 120),
        )

    latest_daily_task = _latest_task_by_type(db, TaskType.DAILY_SUMMARY_GENERATION)
    if latest_daily_task and latest_daily_task.status == TaskStatus.FAILED:
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


def _build_important_events(db: Session, locale: str) -> list[DashboardImportantEvent]:
    importance_score = case(
        (EventRecord.importance_level == "high", 3),
        else_=0,
    )

    rows = (
        db.query(EventRecord, VideoSource)
        .join(VideoSource, EventRecord.source_id == VideoSource.id)
        .filter(_important_condition())
        .order_by(importance_score.desc(), EventRecord.event_start_time.desc())
        .limit(5)
        .all()
    )

    result: list[DashboardImportantEvent] = []
    for event, source in rows:
        result.append(
            DashboardImportantEvent(
                id=event.id,
                title=_build_event_title(event.description, locale),
                summary=_truncate_text(event.description, 60),
                event_time=event.event_start_time,
                camera_name=source.camera_name,
            )
        )

    return result


def _latest_task_by_type(db: Session, task_type: str) -> TaskLog | None:
    return (
        db.query(TaskLog)
        .filter(TaskLog.task_type == task_type)
        .order_by(TaskLog.created_at.desc())
        .first()
    )


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


def _important_condition():
    return EventRecord.importance_level.in_(IMPORTANT_LEVELS)
