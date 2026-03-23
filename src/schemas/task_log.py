from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict


class TaskLogResponse(BaseModel):
    id: int
    task_type: str
    task_target_id: Optional[int] = None
    status: str
    queue_task_id: Optional[str] = None
    cancel_requested: bool = False
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    retry_count: int
    message: Optional[str] = None
    detail_json: Optional[Any] = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
