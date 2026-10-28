from datetime import date as DateType
from datetime import datetime

from pydantic import BaseModel, Field


class DashboardAction(BaseModel):
    label: str
    target: str


class DashboardStatusItem(BaseModel):
    key: str
    label: str
    status: str


class DashboardSystemStatus(BaseModel):
    overall_status: str
    title: str
    description: str
    items: list[DashboardStatusItem]
    primary_action: DashboardAction
    detail_action: DashboardAction


class DashboardAlert(BaseModel):
    show: bool
    type: str | None = None
    title: str | None = None
    description: str | None = None
    action: DashboardAction | None = None


class DashboardTaskSummary(BaseModel):
    last_scan_at: datetime | None = None
    last_analysis_status: str | None = None
    last_daily_summary_status: str | None = None
    failed_task_count_24h: int


class DashboardFocusCount(BaseModel):
    focus_key: str
    label: str
    count: int


class DashboardEventSummary(BaseModel):
    attention_event_count: int
    focus_counts: list[DashboardFocusCount] = Field(default_factory=list)


class DashboardLatestDailySummary(BaseModel):
    exists: bool
    date: DateType | None = None
    status: str
    summary_preview: str | None = None
    empty_reason: str | None = None


class DashboardAttentionEvent(BaseModel):
    id: int
    title: str
    summary: str
    event_time: datetime
    camera_name: str


class DashboardOverviewResponse(BaseModel):
    assistant_name: str
    system_status: DashboardSystemStatus
    alert: DashboardAlert
    task_summary: DashboardTaskSummary
    event_summary: DashboardEventSummary
    latest_daily_summary: DashboardLatestDailySummary
    attention_events: list[DashboardAttentionEvent]
