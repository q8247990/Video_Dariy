from datetime import date as DateType
from datetime import datetime
from typing import Optional

from pydantic import BaseModel


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
    type: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None
    action: Optional[DashboardAction] = None


class DashboardTaskSummary(BaseModel):
    last_scan_at: Optional[datetime] = None
    last_analysis_status: Optional[str] = None
    last_daily_summary_status: Optional[str] = None
    failed_task_count_24h: int


class DashboardEventSummary(BaseModel):
    today_event_count: int
    yesterday_event_count: int
    important_event_count_24h: int


class DashboardLatestDailySummary(BaseModel):
    exists: bool
    date: Optional[DateType] = None
    status: str
    summary_preview: Optional[str] = None
    empty_reason: Optional[str] = None


class DashboardImportantEvent(BaseModel):
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
    important_events: list[DashboardImportantEvent]
