from celery import Celery

from src.core.config import settings

TASK_MODULES = [
    "src.tasks.session_build",
    "src.tasks.analyzer",
    "src.tasks.summarizer",
    "src.tasks.webhook",
    "src.tasks.task_maintenance",
]

celery_app = Celery(
    "worker",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
    include=TASK_MODULES,
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="Asia/Shanghai",
    enable_utc=True,
    task_track_started=True,
    task_time_limit=3600,
    beat_schedule={
        "heartbeat": {
            "task": "src.tasks.task_maintenance.heartbeat",
            "schedule": 60.0,
        },
        "dispatch-scheduled-daily-summary": {
            "task": "src.tasks.summarizer.dispatch_scheduled_daily_summary_task",
            "schedule": 60.0,
        },
    },
)
