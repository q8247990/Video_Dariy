from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class TaskLogResponse(BaseModel):
    id: int
    task_type: str
    task_target_id: int | None = None
    status: str
    queue_task_id: str | None = None
    cancel_requested: bool = False
    started_at: datetime | None = None
    finished_at: datetime | None = None
    retry_count: int
    message: str | None = None
    detail_json: Any | None = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
