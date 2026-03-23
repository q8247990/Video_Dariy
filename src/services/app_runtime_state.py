from typing import Any

from sqlalchemy.orm import Session

from src.models.app_runtime_state import AppRuntimeState


def get_runtime_state(db: Session, key: str) -> Any | None:
    row = db.query(AppRuntimeState).filter(AppRuntimeState.state_key == key).first()
    if row is None:
        return None
    return row.state_value


def set_runtime_state(db: Session, key: str, value: Any) -> None:
    row = db.query(AppRuntimeState).filter(AppRuntimeState.state_key == key).first()
    if row is None:
        db.add(AppRuntimeState(state_key=key, state_value=value))
        return
    row.state_value = value
