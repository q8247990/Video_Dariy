from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.orm import Session

from src.core.config import settings
from src.models.video_file import VideoFile
from src.models.video_session import VideoSession
from src.models.video_session_file_rel import VideoSessionFileRel
from src.services.ffmpeg_utils import run_ffmpeg_concat_to_file


class SessionVideoUnavailableError(ValueError):
    pass


def resolve_video_file_path(video_file: VideoFile) -> Path | None:
    root = Path(settings.VIDEO_ROOT_PATH).resolve()
    candidate = Path(video_file.file_path)
    if not candidate.is_absolute() or ".." in candidate.parts:
        return None
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError:
        return None
    return resolved


def is_video_file_available(video_file: VideoFile) -> bool:
    path = resolve_video_file_path(video_file)
    return path is not None and path.is_file()


def mark_missing_video_file(video_file: VideoFile) -> None:
    if not video_file.file_missing:
        video_file.file_missing = True
        video_file.missing_at = datetime.now(timezone.utc)


def mark_missing_source_video_files(db: Session, source_id: int) -> int:
    marked_count = 0
    video_files = db.query(VideoFile).filter(VideoFile.source_id == source_id).all()
    for video_file in video_files:
        if not is_video_file_available(video_file):
            was_missing = video_file.file_missing
            mark_missing_video_file(video_file)
            marked_count += int(not was_missing)
    return marked_count


def get_session_video_files(db: Session, session_id: int) -> list[VideoFile]:
    session = db.query(VideoSession).filter(VideoSession.id == session_id).first()
    if not session:
        raise SessionVideoUnavailableError(f"Session {session_id} not found")

    rel_rows = (
        db.query(VideoSessionFileRel)
        .filter(VideoSessionFileRel.session_id == session_id)
        .order_by(VideoSessionFileRel.sort_index.asc())
        .all()
    )
    if not rel_rows:
        raise SessionVideoUnavailableError(f"Session {session_id} has no related video files")

    video_file_ids = [rel.video_file_id for rel in rel_rows]
    video_files = db.query(VideoFile).filter(VideoFile.id.in_(video_file_ids)).all()
    video_files_by_id = {video_file.id: video_file for video_file in video_files}

    files: list[VideoFile] = []
    for rel in rel_rows:
        video = video_files_by_id.get(rel.video_file_id)
        if video is not None:
            if not is_video_file_available(video):
                mark_missing_video_file(video)
            files.append(video)

    if not files:
        raise SessionVideoUnavailableError(f"Session {session_id} has no video file records")
    return files


def get_available_session_video_files(db: Session, session_id: int) -> list[VideoFile]:
    files = get_session_video_files(db, session_id)
    available = [video for video in files if is_video_file_available(video)]
    if not available:
        raise SessionVideoUnavailableError(f"Session {session_id} has no available video files")
    return available


def get_merged_video_path(session_id: int) -> Path:
    base_dir = Path(settings.VIDEO_ROOT_PATH)
    if not base_dir.exists():
        base_dir = Path("/tmp/video_dairy")
    merged_dir = base_dir / "session_merged"
    merged_dir.mkdir(parents=True, exist_ok=True)
    return merged_dir / f"session_{session_id}.mp4"


def ensure_merged_video(db: Session, session_id: int) -> str:
    target_path = get_merged_video_path(session_id)
    if target_path.exists() and target_path.stat().st_size > 0:
        return str(target_path)

    video_files = get_available_session_video_files(db, session_id)
    source_paths: list[str] = []
    for video in video_files:
        path = resolve_video_file_path(video)
        if path is not None:
            source_paths.append(str(path))
    if len(source_paths) == 1:
        return source_paths[0]

    run_ffmpeg_concat_to_file(source_paths, str(target_path))
    return str(target_path)
