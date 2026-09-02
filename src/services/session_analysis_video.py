from __future__ import annotations

import base64
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy.orm import Session

from src.services.ffmpeg_utils import run_ffmpeg_concat_to_bytes
from src.services.session_video import get_session_video_files

if TYPE_CHECKING:
    from src.models.video_file import VideoFile
    from src.services.keyframe_extractor import KeyframeSet


def extract_keyframes_for_sub_chunk(
    file_paths: list[str],
    *,
    fps_target: int = 2,
    target_n: int = 64,
    jpeg_quality: int = 88,
    mad_threshold: float = 1.0,
    phash_threshold: int = 6,
    periodic_anchor_seconds: int = 8,
) -> KeyframeSet:
    """关键帧提取的惰性入口（关键帧路径已停用，代码仅为溯源保留）。

    cv2/OpenCV 与 numpy 不在部署依赖中。只有本函数被真正调用时才会导入
    ``keyframe_extractor``，因此导入本模块以及 raw_mp4 主路径均不依赖 cv2。
    """
    from src.services import keyframe_extractor

    return keyframe_extractor.extract_keyframes_for_sub_chunk(
        file_paths,
        fps_target=fps_target,
        target_n=target_n,
        jpeg_quality=jpeg_quality,
        mad_threshold=mad_threshold,
        phash_threshold=phash_threshold,
        periodic_anchor_seconds=periodic_anchor_seconds,
    )


@dataclass
class SessionVideoChunk:
    chunk_index: int
    start_offset_seconds: int
    duration_seconds: int
    file_paths: list[str]
    file_durations: list[int] | None = None


@dataclass
class SubChunk:
    chunk_index: int
    sub_chunk_index: int
    start_offset_seconds: int
    duration_seconds: int
    file_paths: list[str]


@dataclass
class ChunkKeyframePayload:
    jpeg_data_url: str
    media_io_kwargs: dict[str, Any]
    diagnostics: dict[str, Any] = field(default_factory=dict)
    keyframe_set: KeyframeSet | None = None


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
    current_durations: list[int] = []
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
                    file_durations=list(current_durations),
                )
            )
            current_paths = []
            current_durations = []
            current_duration = 0
            current_start_offset = accumulated_offset

        if not current_paths:
            current_start_offset = accumulated_offset

        current_paths.append(video_file.file_path)
        current_durations.append(file_duration)
        current_duration += file_duration
        accumulated_offset += file_duration

    if current_paths:
        chunks.append(
            SessionVideoChunk(
                chunk_index=len(chunks),
                start_offset_seconds=current_start_offset,
                duration_seconds=current_duration,
                file_paths=current_paths,
                file_durations=list(current_durations),
            )
        )

    if not chunks:
        raise ValueError(f"Session {session_id} has no playable video files")
    return chunks


def build_chunk_sub_chunks(
    chunk: SessionVideoChunk,
    db: Session,
    sub_chunk_seconds: int = 300,
) -> list[SubChunk]:
    """将 10 分钟 SessionVideoChunk 切分为 ≤ sub_chunk_seconds 的 SubChunk。

    优先使用 ``chunk.file_durations``（由 ``build_session_video_chunks`` 从
    VideoFile.duration_seconds 填充，避免 ffprobe 子进程开销）。回退到
    60s 估计仅当 file_durations 缺失或长度不匹配 file_paths。
    """
    del db
    if sub_chunk_seconds <= 0:
        raise ValueError("sub_chunk_seconds must be greater than 0")
    if not chunk.file_paths:
        raise ValueError("chunk has no source files")

    if chunk.file_durations is None or len(chunk.file_durations) != len(chunk.file_paths):
        file_durations = [60] * len(chunk.file_paths)
    else:
        file_durations = list(chunk.file_durations)

    sub_chunks: list[SubChunk] = []
    current_paths: list[str] = []
    current_durations: list[int] = []
    current_duration = 0
    current_start_offset = chunk.start_offset_seconds
    accumulated_offset = chunk.start_offset_seconds

    for file_path, file_duration in zip(chunk.file_paths, file_durations, strict=True):
        if current_paths and current_duration + file_duration > sub_chunk_seconds:
            sub_chunks.append(
                SubChunk(
                    chunk_index=chunk.chunk_index,
                    sub_chunk_index=len(sub_chunks),
                    start_offset_seconds=current_start_offset,
                    duration_seconds=current_duration,
                    file_paths=list(current_paths),
                )
            )
            current_paths = []
            current_durations = []
            current_duration = 0
            current_start_offset = accumulated_offset

        if not current_paths:
            current_start_offset = accumulated_offset

        current_paths.append(file_path)
        current_durations.append(file_duration)
        current_duration += file_duration
        accumulated_offset += file_duration

    if current_paths:
        sub_chunks.append(
            SubChunk(
                chunk_index=chunk.chunk_index,
                sub_chunk_index=len(sub_chunks),
                start_offset_seconds=current_start_offset,
                duration_seconds=current_duration,
                file_paths=list(current_paths),
            )
        )

    return sub_chunks


def build_chunk_keyframe_payload(
    sub_chunk: SubChunk,
    *,
    target_n: int = 64,
    jpeg_quality: int = 88,
    mad_threshold: float = 1.0,
    phash_threshold: int = 6,
    periodic_anchor_seconds: int = 8,
) -> ChunkKeyframePayload:
    """提取 sub_chunk 的关键帧，组装 data URL + media_io_kwargs。"""
    ks: KeyframeSet = extract_keyframes_for_sub_chunk(
        sub_chunk.file_paths,
        target_n=target_n,
        jpeg_quality=jpeg_quality,
        mad_threshold=mad_threshold,
        phash_threshold=phash_threshold,
        periodic_anchor_seconds=periodic_anchor_seconds,
    )

    joined_b64 = ",".join(ks.jpeg_base64_list)
    data_url = f"data:video/jpeg;base64,{joined_b64}" if joined_b64 else ""

    media_io_kwargs: dict[str, Any] = {
        "video": {
            "fps": ks.fps,
            "total_num_frames": ks.total_num_frames,
            "frames_indices": list(ks.frames_indices),
            "num_frames": -1,
        }
    }
    return ChunkKeyframePayload(
        jpeg_data_url=data_url,
        media_io_kwargs=media_io_kwargs,
        diagnostics=ks.extra,
        keyframe_set=ks,
    )


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


def session_chunk_from_sub_chunk(sub_chunk: SubChunk, parent_chunk_index: int) -> SessionVideoChunk:
    """Project a SubChunk back to a SessionVideoChunk shape (for raw_mp4 fallback)."""
    return SessionVideoChunk(
        chunk_index=parent_chunk_index,
        start_offset_seconds=sub_chunk.start_offset_seconds,
        duration_seconds=sub_chunk.duration_seconds,
        file_paths=list(sub_chunk.file_paths),
    )


def _resolve_file_duration_seconds(video_file: VideoFile) -> int:
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


__all__ = [
    "ChunkKeyframePayload",
    "SessionVideoChunk",
    "SubChunk",
    "build_chunk_keyframe_payload",
    "build_chunk_sub_chunks",
    "build_chunk_video_data_url",
    "build_session_video_chunks",
    "session_chunk_from_sub_chunk",
]
