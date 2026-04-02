import os
from pathlib import Path

from sqlalchemy.orm import Session

from src.core.config import settings
from src.models.video_file import VideoFile
from src.models.video_session import VideoSession
from src.models.video_session_file_rel import VideoSessionFileRel
from src.services.ffmpeg_utils import run_ffmpeg_concat_to_file


def get_session_video_files(db: Session, session_id: int) -> list[VideoFile]:
    session = db.query(VideoSession).filter(VideoSession.id == session_id).first()
    if not session:
        raise ValueError(f"Session {session_id} not found")

    rel_rows = (
        db.query(VideoSessionFileRel)
        .filter(VideoSessionFileRel.session_id == session_id)
        .order_by(VideoSessionFileRel.sort_index.asc())
        .all()
    )
    if not rel_rows:
        raise ValueError(f"Session {session_id} has no related video files")

    files: list[VideoFile] = []
    for rel in rel_rows:
        video = db.query(VideoFile).filter(VideoFile.id == rel.video_file_id).first()
        if video:
            files.append(video)

    if not files:
        raise ValueError(f"Session {session_id} has no playable video files")
    return files


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

    video_files = get_session_video_files(db, session_id)
    source_paths: list[str] = []
    for video in video_files:
        if not video.file_path or not os.path.exists(video.file_path):
            raise ValueError(f"Video file missing on disk: {video.file_path}")
        source_paths.append(video.file_path)

    if len(source_paths) == 1:
        return source_paths[0]

    run_ffmpeg_concat_to_file(source_paths, str(target_path))
    return str(target_path)


def _build_concat_file(paths: list[str]) -> str:
    import shlex
    import tempfile

    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8")
    try:
        for path in paths:
            escaped_path = shlex.quote(path)
            tmp.write(f"file {escaped_path}\n")
    finally:
        tmp.close()
    return tmp.name
