"""Video source runtime state helpers."""

from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from src.models.video_source_runtime_state import VideoSourceRuntimeState


def get_or_create_runtime_state(db: Session, source_id: int) -> VideoSourceRuntimeState:
    state = (
        db.query(VideoSourceRuntimeState)
        .filter(VideoSourceRuntimeState.source_id == source_id)
        .first()
    )
    if state is not None:
        return state

    state = VideoSourceRuntimeState(source_id=source_id)
    db.add(state)
    db.flush()
    return state


def get_alert_state(
    db: Session,
    source_id: int,
    alert_type: str,
) -> tuple[int, bool, Optional[datetime]]:
    state = get_or_create_runtime_state(db, source_id)
    if alert_type == "latency":
        return (
            int(state.latency_alert_counter),
            bool(state.latency_alert_active),
            state.latency_alert_last_notified_at,
        )
    raise ValueError(f"unsupported alert_type: {alert_type}")


def set_alert_state(
    db: Session,
    source_id: int,
    alert_type: str,
    *,
    counter: Optional[int] = None,
    active: Optional[bool] = None,
    last_notified_at: Optional[datetime] = None,
) -> None:
    state = get_or_create_runtime_state(db, source_id)
    if alert_type == "latency":
        if counter is not None:
            state.latency_alert_counter = counter
        if active is not None:
            state.latency_alert_active = active
        if last_notified_at is not None:
            state.latency_alert_last_notified_at = last_notified_at
        return
    raise ValueError(f"unsupported alert_type: {alert_type}")
