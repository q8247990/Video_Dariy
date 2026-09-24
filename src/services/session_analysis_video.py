"""File-level session analysis helpers.

Session analysis treats every video file as exactly one work unit:
**one file per sub-chunk, one vision-model call per sub-chunk**. The
historical two-level split — a 600 s ``SessionVideoChunk`` layer that
was then packed into ``<= 60 s`` sub-chunks with a cross-file
``ffmpeg concat`` fallback — was removed: the 600 s layer never
reached the model, and the packing merged short files into a single
call. See ``docs/todo.md`` for the decision record.

Payloads are always ``data:video/mp4`` with a ``num_frames`` hint
(``RAW_MP4_NUM_FRAMES``). The historical keyframe path lives only in
ADR ``0009-remove-keyframe-pipeline.md``; nothing here imports ``cv2``
or ``numpy``.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

from src.services.session_video import get_session_video_files

if TYPE_CHECKING:
    from src.models.video_file import VideoFile


@dataclass
class SubChunk:
    """One work unit: a single video file and its session-relative span.

    ``chunk_index`` is retained (always ``0``) so the persisted
    ``session_analysis_checkpoint`` work key keeps its existing
    ``(session_id, analysis_run_id, chunk_index, sub_chunk_index)``
    shape without a schema migration; ``sub_chunk_index`` is the file's
    ordinal position inside the session.
    """

    chunk_index: int
    sub_chunk_index: int
    start_offset_seconds: int
    duration_seconds: int
    file_paths: list[str]


def build_file_sub_chunks(db: Session, session_id: int) -> list[SubChunk]:
    """Return one :class:`SubChunk` per session video file, in order.

    Each sub-chunk carries exactly one file path and the session-relative
    start offset accumulated from the preceding files' durations. No file
    is ever split and no two files are ever merged.
    """
    video_files = get_session_video_files(db, session_id)
    sub_chunks: list[SubChunk] = []
    accumulated_offset = 0
    for index, video_file in enumerate(video_files):
        if not video_file.file_path:
            raise ValueError(f"Video file path is empty: {video_file.id}")
        duration_seconds = _resolve_file_duration_seconds(video_file)
        sub_chunks.append(
            SubChunk(
                chunk_index=0,
                sub_chunk_index=index,
                start_offset_seconds=accumulated_offset,
                duration_seconds=duration_seconds,
                file_paths=[video_file.file_path],
            )
        )
        accumulated_offset += duration_seconds
    if not sub_chunks:
        raise ValueError(f"Session {session_id} has no playable video files")
    return sub_chunks


def build_video_data_url(file_path: str) -> str:
    """Read one mp4 from disk and wrap it as a ``data:video/mp4`` URL."""
    video_bytes = Path(file_path).read_bytes()
    if not video_bytes:
        raise ValueError(f"video file {file_path} produced empty video bytes")
    video_base64 = base64.b64encode(video_bytes).decode("utf-8")
    return f"data:video/mp4;base64,{video_base64}"


def _resolve_file_duration_seconds(video_file: VideoFile) -> int:
    duration_seconds = video_file.duration_seconds
    if isinstance(duration_seconds, (int, float)) and duration_seconds > 0:
        return int(duration_seconds)

    estimated = int((video_file.end_time - video_file.start_time).total_seconds())
    if estimated > 0:
        return estimated
    return 60


__all__ = [
    "SubChunk",
    "build_file_sub_chunks",
    "build_video_data_url",
]
