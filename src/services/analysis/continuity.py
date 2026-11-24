"""Identity-continuity helpers shared by the analysis orchestration."""

from __future__ import annotations

from sqlalchemy.orm import Session

from src.models.video_session import VideoSession

_MAX_SUBJECTS = 8


def load_previous_session_subjects(db: Session, session: VideoSession) -> list[str]:
    """Return the on-screen subjects of the session immediately before ``session``."""
    previous = (
        db.query(VideoSession)
        .filter(
            VideoSession.source_id == session.source_id,
            VideoSession.session_start_time < session.session_start_time,
            VideoSession.id != session.id,
        )
        .order_by(VideoSession.session_start_time.desc())
        .first()
    )
    if previous is None:
        return []
    raw = previous.main_subjects_json
    if not isinstance(raw, list):
        return []
    subjects: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        name = item.strip()
        if name and name not in subjects:
            subjects.append(name)
    return subjects[:_MAX_SUBJECTS]


__all__ = ["load_previous_session_subjects"]
