from datetime import datetime, timedelta

from sqlalchemy import case, func
from sqlalchemy.orm import Session

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


def get_dashboard_overview(db: Session) -> DashboardOverviewResponse:
    onboarding_status = get_onboarding_status(db)
    assistant_name = _get_assistant_name(db)

    return DashboardOverviewResponse(
        assistant_name=assistant_name,
        system_status=_build_system_status(onboarding_status),
        alert=_build_alert(db, onboarding_status),
        task_summary=_build_task_summary(db),
        event_summary=_build_event_summary(db),
        latest_daily_summary=_build_latest_daily_summary(db),
        important_events=_build_important_events(db),
    )


def _get_assistant_name(db: Session) -> str:
    profile = db.query(HomeProfile).order_by(HomeProfile.id.asc()).first()
    if profile and (profile.assistant_name or "").strip():
        return profile.assistant_name.strip()
    return DEFAULT_ASSISTANT_NAME


def _build_system_status(onboarding_status: dict) -> DashboardSystemStatus:
    overall_status = onboarding_status["overall_status"]
    next_action = str(onboarding_status.get("next_action") or "")

    title_map = {
        "basic_not_ready": "未完成基础配置",
        "basic_ready": "基础可运行",
        "full_ready": "完整可运行",
    }
    description_map = {
        "basic_not_ready": "尚未完成基础配置，系统暂不能进行完整分析",
        "basic_ready": "系统已可开始分析，建议继续完善家庭信息与风格设置",
        "full_ready": "系统已完成主要配置，正在持续分析家庭监控视频",
    }

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
            label="视频源",
            status=_bool_pair_to_status(video_configured, video_validated),
        ),
        DashboardStatusItem(
            key="provider",
            label="Provider",
            status=_bool_pair_to_status(provider_configured, provider_tested),
        ),
        DashboardStatusItem(
            key="daily_summary",
            label="日报",
            status="ok" if daily_summary_configured else "not_ready",
        ),
        DashboardStatusItem(
            key="home_profile",
            label="家庭档案",
            status="ok" if home_profile_configured else "partial",
        ),
    ]

    if overall_status == "basic_not_ready":
        primary_action = DashboardAction(
            label="继续完成初始化",
            target=_onboarding_target_from_action(next_action),
        )
    elif overall_status == "basic_ready":
        primary_action = DashboardAction(
            label="完善家庭配置", target="/onboarding/personalize/profile"
        )
    else:
        primary_action = DashboardAction(label="查看状态详情", target="/system-status")

    return DashboardSystemStatus(
        overall_status=overall_status,
        title=title_map[overall_status],
        description=description_map[overall_status],
        items=items,
        primary_action=primary_action,
        detail_action=DashboardAction(label="查看状态详情", target="/system-status"),
    )


def _build_alert(db: Session, onboarding_status: dict) -> DashboardAlert:
    if onboarding_status["overall_status"] == "basic_not_ready":
        return DashboardAlert(
            show=True,
            type="basic_not_ready",
            title="尚未完成基础配置",
            description="系统暂不能进行完整分析，建议继续完成初始化配置",
            action=DashboardAction(label="继续完成初始化", target="/onboarding"),
        )

    steps = onboarding_status["steps"]
    if not steps["provider"]["configured"] or not steps["provider"]["tested"]:
        return DashboardAlert(
            show=True,
            type="provider_error",
            title="Provider 异常",
            description="当前模型服务不可用，可能影响分析与日报生成",
            action=DashboardAction(label="去检查 Provider", target="/providers"),
        )

    if not steps["video_source"]["configured"] or not steps["video_source"]["validated"]:
        return DashboardAlert(
            show=True,
            type="video_source_error",
            title="视频源异常",
            description="视频源配置未完成或校验失败，可能无法继续扫描分析",
            action=DashboardAction(label="去检查视频源", target="/video-sources"),
        )

    latest_daily_task = _latest_task_by_type(db, TaskType.DAILY_SUMMARY_GENERATION)
    if latest_daily_task and latest_daily_task.status == TaskStatus.FAILED:
        return DashboardAlert(
            show=True,
            type="daily_summary_error",
            title="日报任务异常",
            description="最近一次日报生成失败，建议检查日志并重试",
            action=DashboardAction(label="查看任务日志", target="/tasks"),
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
            title="分析任务异常",
            description=f"最近24小时有 {failed_analysis_count} 个分析任务失败",
            action=DashboardAction(
                label="查看任务日志", target="/tasks?status=failed&task_type=session_analysis"
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


def _build_important_events(db: Session) -> list[DashboardImportantEvent]:
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
                title=_build_event_title(event.description),
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


def _build_event_title(description: str) -> str:
    text = description.strip()
    if not text:
        return "未命名事件"
    if len(text) <= 18:
        return text
    return f"{text[:18].rstrip()}..."


def _important_condition():
    return EventRecord.importance_level.in_(IMPORTANT_LEVELS)
