import base64
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from src.services.ffmpeg_utils import run_ffmpeg_concat_to_bytes
from src.services.session_video import get_session_video_files


@dataclass
class SessionVideoChunk:
    chunk_index: int
    start_offset_seconds: int
    duration_seconds: int
    file_paths: list[str]


def build_session_video_chunks(
    db: Session,
    session_id: int,
    chunk_seconds: int = 600,
) -> list[SessionVideoChunk]:
    if chunk_seconds <= 0:
        raise ValueError("chunk_seconds must be greater than 0")

    video_files = get_session_video_files(db, session_id)
    chunks: list[SessionVideoChunk] = []
    current_paths: list[str] = []
    current_duration = 0
    current_start_offset = 0
    accumulated_offset = 0

    for video_file in video_files:
        file_duration = _resolve_file_duration_seconds(video_file)
        if not video_file.file_path:
            raise ValueError(f"Video file path is empty: {video_file.id}")

        if current_paths and current_duration + file_duration > chunk_seconds:
            chunks.append(
                SessionVideoChunk(
                    chunk_index=len(chunks),
                    start_offset_seconds=current_start_offset,
                    duration_seconds=current_duration,
                    file_paths=current_paths,
                )
            )
            current_paths = []
            current_duration = 0
            current_start_offset = accumulated_offset

        if not current_paths:
            current_start_offset = accumulated_offset

        current_paths.append(video_file.file_path)
        current_duration += file_duration
        accumulated_offset += file_duration

    if current_paths:
        chunks.append(
            SessionVideoChunk(
                chunk_index=len(chunks),
                start_offset_seconds=current_start_offset,
                duration_seconds=current_duration,
                file_paths=current_paths,
            )
        )

    if not chunks:
        raise ValueError(f"Session {session_id} has no playable video files")
    return chunks


def build_chunk_video_data_url(chunk: SessionVideoChunk) -> str:
    if not chunk.file_paths:
        raise ValueError("chunk has no source files")

    if len(chunk.file_paths) == 1:
        source_path = chunk.file_paths[0]
        video_bytes = Path(source_path).read_bytes()
    else:
        video_bytes = _concat_video_files_to_mp4_bytes(chunk.file_paths)

    if not video_bytes:
        raise ValueError(f"chunk {chunk.chunk_index} produced empty video bytes")

    video_base64 = base64.b64encode(video_bytes).decode("utf-8")
    return f"data:video/mp4;base64,{video_base64}"


def _resolve_file_duration_seconds(video_file) -> int:
    duration_seconds = video_file.duration_seconds
    if isinstance(duration_seconds, (int, float)) and duration_seconds > 0:
        return int(duration_seconds)

    estimated = int((video_file.end_time - video_file.start_time).total_seconds())
    if estimated > 0:
        return estimated
    return 60


def _concat_video_files_to_mp4_bytes(file_paths: list[str]) -> bytes:
    return run_ffmpeg_concat_to_bytes(file_paths)


def _build_concat_payload(file_paths: list[str]) -> bytes:
    lines = []
    for path in file_paths:
        escaped_path = path.replace("'", "'\\''")
        lines.append(f"file '{escaped_path}'")
    return ("\n".join(lines) + "\n").encode("utf-8")
