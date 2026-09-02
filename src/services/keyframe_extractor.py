"""ffmpeg 单遍关键帧提取 (REPORT §4.1)。

按 REPORT §4.1 的客户端预处理管线：从源 mp4 单遍解码，2fps 采样 + 在线
MAD/pHash 决策，输出 top-N JPEG 关键帧与 media_io_kwargs 元数据。
vLLM 端零改动。

REPORT §7 约束：
- frames_indices 升序且与 jpeg_base64_list 一一对应
- num_frames=-1 必须显式写入 media_io_kwargs.video
- fps 单值（用第一片 fps）
- jpeg_quality >= 85，N <= 768
- N 必须偶数（temporal_patch_size=2）
"""

from __future__ import annotations

import base64
import json
import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

import cv2
import numpy as np

logger = logging.getLogger(__name__)

_JPEG_WIDTH = 1920
_JPEG_HEIGHT = 1080
_SMALL_WIDTH = 160
_SMALL_HEIGHT = 90
_PHASH_DIM = 32
_STDERR_TAIL_BYTES = 1024


class KeyframeExtractionError(Exception):
    """关键帧提取失败时抛出（含 ffmpeg stderr 摘要）。"""


@dataclass(frozen=True)
class KeyframeSet:
    """单次关键帧提取的产出（按时间顺序，frames_indices 升序）。

    Attributes:
        jpeg_base64_list: JPEG base64 字符串列表（不含 data: 前缀），与
            frames_indices 一一对应，按时间顺序排列。
        fps: 整个 sub-chunk 的基准 fps（使用第一片 fps）。
        total_num_frames: 拼接时间轴的总帧数。
        frames_indices: 每个 JPEG 在拼接时间轴上的绝对帧号；必须严格升序。
        sample_period_frames: 周期锚点间隔帧数（periodic_anchor_seconds × fps）。
        source_duration_seconds: 输入 sub-chunk 的总时长（秒）。
        extra: 诊断信息，供 TaskLog.detail_json 使用。
    """

    jpeg_base64_list: list[str]
    fps: float
    total_num_frames: int
    frames_indices: list[int]
    sample_period_frames: int
    source_duration_seconds: float
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class _Candidate:
    index_in_chunk: int
    score: float
    jpeg_base64: str


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
    """提取 sub-chunk 的 top-N 关键帧（内存中，不落盘）。

    流程（REPORT §4.1）：
    1. 对每个源文件 ffmpeg 单遍解码（2fps bgr24 pipe:1），累加 absolute
       frame offset；每个源片段仅一次解码。
    2. 每帧：to_small_gray + phash64 + score_frame（与前一帧比较）。
    3. 候选判定：mad > threshold 或 phd > threshold；周期锚点每
       periodic_anchor_seconds × fps_target 帧强制一个候选。
    4. 候选帧 → 1920x1080 JPEG q88 → base64（立即丢弃原 BGR 帧）。
    5. 全部候选 → top-N by change_score → 强制偶数 → 组装 KeyframeSet。

    Raises:
        KeyframeExtractionError: ffmpeg 调用失败或文件不存在。
        AssertionError: REPORT §7 参数越界（由 caller 立即可见）。
    """
    assert target_n > 0, f"target_n must be > 0, got {target_n}"
    assert target_n <= 768, f"target_n must be <= 768, got {target_n}"
    assert jpeg_quality >= 85, f"jpeg_quality must be >= 85, got {jpeg_quality}"
    assert file_paths, "file_paths must be non-empty"

    fps_values: list[float] = []
    durations: list[float] = []
    for path_str in file_paths:
        path = Path(path_str)
        if not path.is_file():
            raise KeyframeExtractionError(f"input file not found: {path}")
        fps, dur = _probe_video(path)
        fps_values.append(fps)
        durations.append(dur)

    baseline_fps = fps_values[0]
    sample_period_frames = max(1, int(round(periodic_anchor_seconds * fps_target)))
    total_num_frames = sum(int(round(dur * fps_target)) for dur in durations)

    candidates: list[_Candidate] = []
    samples_since_anchor = 0
    absolute_frame_offset = 0
    extra_counts: dict[str, int] = {
        "frames_decoded": 0,
        "change_points": 0,
        "periodic_anchors": 0,
    }

    prev_small: np.ndarray | None = None
    prev_hash: int | None = None

    for path_str, dur in zip(file_paths, durations, strict=True):
        path = Path(path_str)
        path_frames = int(round(dur * fps_target))
        file_frame_index = 0
        for frame_bgr in _decode_file_with_ffmpeg_pipe(path, fps_target):
            extra_counts["frames_decoded"] += 1
            cur_small = _to_small_gray(frame_bgr)
            cur_hash = _phash64(cur_small)
            samples_since_anchor += 1

            is_change, mad, phd = _score_frame(
                prev_small,
                cur_small,
                prev_hash,
                cur_hash,
                mad_threshold=mad_threshold,
                phash_threshold=phash_threshold,
            )
            is_periodic = _maybe_periodic_anchor(
                samples_since_anchor,
                fps_target=fps_target,
                periodic_anchor_seconds=periodic_anchor_seconds,
            )
            if is_periodic:
                samples_since_anchor = 0

            if is_change or is_periodic:
                if is_change:
                    extra_counts["change_points"] += 1
                if is_periodic:
                    extra_counts["periodic_anchors"] += 1
                jpeg_b64 = _emit_jpeg_base64(frame_bgr, jpeg_quality=jpeg_quality)
                candidates.append(
                    _Candidate(
                        index_in_chunk=absolute_frame_offset + file_frame_index,
                        score=mad + float(phd),
                        jpeg_base64=jpeg_b64,
                    )
                )

            prev_small = cur_small
            prev_hash = cur_hash
            file_frame_index += 1

        absolute_frame_offset += path_frames

    top = _top_n_by_change_score(candidates, target_n)
    if not top:
        return KeyframeSet(
            jpeg_base64_list=[],
            fps=baseline_fps,
            total_num_frames=total_num_frames,
            frames_indices=[],
            sample_period_frames=sample_period_frames,
            source_duration_seconds=float(total_num_frames) / max(baseline_fps, 1e-6),
            extra=extra_counts,
        )

    top.sort(key=lambda c: c.index_in_chunk)
    indices = [c.index_in_chunk for c in top]
    jpegs = [c.jpeg_base64 for c in top]
    assert indices == sorted(indices), "frames_indices must be ascending"
    assert len(set(indices)) == len(indices), "frames_indices must be unique"
    assert len(jpegs) % 2 == 0, "jpeg count must be even (temporal_patch_size=2)"

    return KeyframeSet(
        jpeg_base64_list=jpegs,
        fps=baseline_fps,
        total_num_frames=total_num_frames,
        frames_indices=indices,
        sample_period_frames=sample_period_frames,
        source_duration_seconds=float(total_num_frames) / max(baseline_fps, 1e-6),
        extra=extra_counts,
    )


def _probe_video(path: Path) -> tuple[float, float]:
    """ffprobe 读取 fps 和 duration（秒）。"""
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=avg_frame_rate,duration",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired as exc:
        raise KeyframeExtractionError(f"ffprobe timeout for {path}") from exc
    except FileNotFoundError as exc:
        raise KeyframeExtractionError("ffprobe not installed") from exc
    if result.returncode != 0:
        raise KeyframeExtractionError(
            f"ffprobe failed for {path}: {result.stderr[-_STDERR_TAIL_BYTES:]}"
        )
    try:
        data = json.loads(result.stdout or "{}")
        stream = data["streams"][0]
        rate = stream.get("avg_frame_rate", "0/1")
        num_str, den_str = rate.split("/")
        num = float(num_str)
        den = float(den_str) if float(den_str) != 0 else 1.0
        fps = num / den
        dur = float(stream.get("duration", 0.0))
        return fps, dur
    except (KeyError, ValueError, IndexError, json.JSONDecodeError) as exc:
        raise KeyframeExtractionError(
            f"ffprobe output parse failed for {path}: {exc}; "
            f"stdout={result.stdout[:_STDERR_TAIL_BYTES]}"
        ) from exc


def _decode_file_with_ffmpeg_pipe(path: Path, fps_target: int) -> Iterator[np.ndarray]:
    """对单个 mp4 启动 ffmpeg 单遍解码，yield 原始 BGR 帧。"""
    width, height = _detect_dimensions(path)
    frame_size = width * height * 3
    cmd = [
        "ffmpeg",
        "-loglevel",
        "error",
        "-i",
        str(path),
        "-vf",
        f"fps={fps_target}",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "bgr24",
        "pipe:1",
    ]
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=frame_size * 4,
        )
    except FileNotFoundError as exc:
        raise KeyframeExtractionError("ffmpeg not installed") from exc
    assert proc.stdout is not None
    try:
        while True:
            buf = proc.stdout.read(frame_size)
            if len(buf) < frame_size:
                break
            frame = np.frombuffer(buf, dtype=np.uint8).reshape((height, width, 3))
            yield frame
    finally:
        try:
            proc.stdout.close()
        except Exception:  # noqa: BLE001
            pass
        stderr_tail = b""
        if proc.stderr is not None:
            try:
                stderr_tail = proc.stderr.read()
            except Exception:  # noqa: BLE001
                pass
            try:
                proc.stderr.close()
            except Exception:  # noqa: BLE001
                pass
        returncode = proc.wait(timeout=10)
        if returncode != 0:
            tail = stderr_tail[-_STDERR_TAIL_BYTES:].decode("utf-8", errors="ignore")
            logger.warning(
                "ffmpeg decode failed for %s (exit=%s): %s",
                path,
                returncode,
                tail,
            )
            raise KeyframeExtractionError(
                f"ffmpeg decode failed for {path} (exit={returncode}): {tail}"
            )


def _detect_dimensions(path: Path) -> tuple[int, int]:
    """ffprobe 读取 width / height。"""
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        raise KeyframeExtractionError(f"ffprobe dimensions failed: {exc}") from exc
    if result.returncode != 0:
        raise KeyframeExtractionError(
            f"ffprobe dimensions failed: {result.stderr[-_STDERR_TAIL_BYTES:]}"
        )
    try:
        data = json.loads(result.stdout or "{}")
        stream = data["streams"][0]
        return int(stream["width"]), int(stream["height"])
    except (KeyError, ValueError, IndexError, json.JSONDecodeError) as exc:
        raise KeyframeExtractionError(f"ffprobe output parse: {exc}") from exc


def _to_small_gray(frame_bgr: np.ndarray) -> np.ndarray:
    """BGR → 160x90 灰度，用于 MAD / pHash。"""
    small = cv2.resize(frame_bgr, (_SMALL_WIDTH, _SMALL_HEIGHT), interpolation=cv2.INTER_AREA)
    return cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)


def _phash64(small_gray: np.ndarray) -> int:
    """160x90 灰度 → 64-bit pHash（DCT-based）。"""
    resized = cv2.resize(small_gray, (_PHASH_DIM, _PHASH_DIM), interpolation=cv2.INTER_AREA)
    resized_f: np.ndarray = np.asarray(resized, dtype=np.float64)
    dct = cv2.dct(resized_f)
    low = np.asarray(dct[:8, :8]).reshape(-1).astype(np.float64)
    med = float(np.median(low[1:]))
    bits = (low > med).astype(np.uint8)
    out = 0
    for bit in bits:
        out = (out << 1) | int(bit)
    return out


def _phash_distance(prev_hash: int, cur_hash: int) -> int:
    """两个 64-bit pHash 之间的汉明距离。"""
    return bin(prev_hash ^ cur_hash).count("1")


def _score_frame(
    prev_small: np.ndarray | None,
    cur_small: np.ndarray,
    prev_hash: int | None,
    cur_hash: int,
    *,
    mad_threshold: float,
    phash_threshold: int,
) -> tuple[bool, float, int]:
    """计算 (is_change_point, mad, phd)。"""
    if prev_small is None or prev_hash is None:
        return False, 0.0, 0
    diff = cv2.absdiff(prev_small, cur_small)
    mad = float(np.mean(diff.astype(np.float64)))
    phd = _phash_distance(prev_hash, cur_hash)
    is_change = mad > mad_threshold or phd > phash_threshold
    return is_change, mad, phd


def _emit_jpeg_base64(frame_bgr: np.ndarray, *, jpeg_quality: int) -> str:
    """BGR 帧 → 1920x1080 JPEG q88 → base64 字符串（不含 data: 前缀）。"""
    resized = cv2.resize(frame_bgr, (_JPEG_WIDTH, _JPEG_HEIGHT), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(
        ".jpg",
        resized,
        [int(cv2.IMWRITE_JPEG_QUALITY), int(jpeg_quality)],
    )
    if not ok:
        raise KeyframeExtractionError("cv2.imencode failed")
    return base64.b64encode(buf.tobytes()).decode("ascii")


def _maybe_periodic_anchor(
    samples_since_anchor: int,
    *,
    fps_target: int,
    periodic_anchor_seconds: int,
) -> bool:
    """每 periodic_anchor_seconds × fps_target 帧强制锚点一个候选帧。"""
    threshold = max(1, int(round(periodic_anchor_seconds * fps_target)))
    return samples_since_anchor >= threshold


def _top_n_by_change_score(candidates: list[_Candidate], n: int) -> list[_Candidate]:
    """从候选列表中选 top-N（变化量降序），并强制偶数（必要时减 1）。"""
    if n <= 0 or not candidates:
        return []
    ranked = sorted(candidates, key=lambda c: c.score, reverse=True)
    keep = min(n, len(ranked))
    if keep % 2 == 1:
        keep -= 1
    return ranked[:keep]


def _frame_index_for(time_seconds: float, fps: float) -> int:
    """给定时间（秒）+ fps，返回绝对帧号。"""
    return int(round(time_seconds * fps))


__all__ = [
    "KeyframeSet",
    "KeyframeExtractionError",
    "extract_keyframes_for_sub_chunk",
]
