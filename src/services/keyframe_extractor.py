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

实际算法实现由 Wave 2 任务 T1.2-T1.9 填充；本模块提供稳定导入面与
REPORT §7 运行时 guards。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


class KeyframeExtractionError(Exception):
    """关键帧提取失败时抛出（含 ffmpeg stderr 摘要）。"""


@dataclass(frozen=True)
class KeyframeSet:
    """单次关键帧提取的产出（按时间顺序，frames_indices 升序）。

    Attributes:
        jpeg_base64_list: JPEG base64 字符串列表（不含 data: 前缀），与
            frames_indices 一一对应，按时间顺序排列。
        fps: 整个 sub-chunk 的基准 fps（使用第一片 fps）。
        total_num_frames: 拼接时间轴的总帧数（Σ round(dur_k × fps)）。
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

    实现见 Wave 2 T1.2-T1.9。本函数入口的 REPORT §7 guards 立刻生效。
    """
    assert target_n > 0, f"target_n must be > 0, got {target_n}"
    assert target_n <= 768, f"target_n must be <= 768, got {target_n}"
    assert jpeg_quality >= 85, f"jpeg_quality must be >= 85, got {jpeg_quality}"
    raise NotImplementedError("Wave 2 T1.2-T1.9 will implement algorithm")


def _decode_file_with_ffmpeg_pipe(path: Any, fps_target: int) -> Any:
    """对单个 mp4 启动 ffmpeg 单遍解码，yield 原始 BGR 帧。"""
    raise NotImplementedError


def _to_small_gray(frame_bgr: Any) -> Any:
    """BGR → 160x90 灰度，用于 MAD 计算。"""
    raise NotImplementedError


def _phash64(small_gray: Any) -> Any:
    """160x90 灰度 → 64-bit pHash。"""
    raise NotImplementedError


def _phash_distance(prev_hash: Any, cur_hash: Any) -> int:
    """两个 pHash 之间的汉明距离（0-64）。"""
    raise NotImplementedError


def _score_frame(
    prev_small: Any,
    cur_small: Any,
    prev_hash: Any,
    cur_hash: Any,
    *,
    mad_threshold: float,
    phash_threshold: int,
) -> tuple[bool, float, int]:
    """计算 (is_change_point, mad, phd)。"""
    raise NotImplementedError


def _emit_jpeg_base64(frame_bgr: Any, *, jpeg_quality: int) -> str:
    """BGR 帧 → 1920x1080 JPEG q88 → base64 字符串（不含 data: 前缀）。"""
    raise NotImplementedError


def _maybe_periodic_anchor(
    samples_since_anchor: int,
    *,
    fps_target: int,
    periodic_anchor_seconds: int,
) -> bool:
    """每 periodic_anchor_seconds × fps_target 帧强制锚点一个候选帧。"""
    raise NotImplementedError


def _top_n_by_change_score(candidates: list[Any], n: int) -> list[Any]:
    """从候选列表中选 top-N（变化量降序），并强制偶数（必要时减 1）。"""
    raise NotImplementedError


def _frame_index_for(time_seconds: float, fps: float) -> int:
    """给定时间（秒）+ fps，返回绝对帧号。"""
    raise NotImplementedError


__all__ = [
    "KeyframeSet",
    "KeyframeExtractionError",
    "extract_keyframes_for_sub_chunk",
]
