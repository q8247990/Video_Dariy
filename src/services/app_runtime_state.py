from typing import Any

from sqlalchemy.exc import IntegrityError
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


def claim_runtime_state(db: Session, key: str, value: Any) -> bool:
    """Atomically create a state key, returning whether this caller acquired it."""
    try:
        with db.begin_nested():
            db.add(AppRuntimeState(state_key=key, state_value=value))
            db.flush()
    except IntegrityError:
        return False
    return True


def clear_runtime_state(db: Session, key: str) -> None:
    """Remove a state key so a failed operation can be retried."""
    db.query(AppRuntimeState).filter(AppRuntimeState.state_key == key).delete()
