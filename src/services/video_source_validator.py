import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from src.adapters.xiaomi_parser import XiaomiDirectoryParser
from src.core.config import settings


@dataclass
class VideoPathValidationResult:
    valid: bool
    message: str
    file_count: int = 0
    earliest_file_time: Optional[datetime] = None
    latest_file_time: Optional[datetime] = None
    resolved_path: Optional[str] = None


def validate_video_source_path(path_text: str) -> VideoPathValidationResult:
    resolved_path = _resolve_allowed_path(path_text)
    if resolved_path is None:
        return VideoPathValidationResult(
            valid=False,
            message="path is outside VIDEO_ROOT_PATH",
        )

    if not resolved_path.exists():
        return VideoPathValidationResult(
            valid=False,
            message="path does not exist",
            resolved_path=str(resolved_path),
        )

    if not resolved_path.is_dir():
        return VideoPathValidationResult(
            valid=False,
            message="path is not a directory",
            resolved_path=str(resolved_path),
        )

    if not os.access(str(resolved_path), os.R_OK | os.X_OK):
        return VideoPathValidationResult(
            valid=False,
            message="path is not readable",
            resolved_path=str(resolved_path),
        )

    parser = XiaomiDirectoryParser(str(resolved_path))
    records = parser.scan_directory()
    if not records:
        return VideoPathValidationResult(
            valid=False,
            message="no supported video files found",
            resolved_path=str(resolved_path),
        )

    start_times = [item["start_time"] for item in records]
    return VideoPathValidationResult(
        valid=True,
        message="path exists and readable",
        file_count=len(records),
        earliest_file_time=min(start_times),
        latest_file_time=max(start_times),
        resolved_path=str(resolved_path),
    )


def _resolve_allowed_path(path_text: str) -> Optional[Path]:
    if not path_text or not path_text.strip():
        return None

    base = Path(settings.VIDEO_ROOT_PATH).resolve()
    candidate = Path(path_text.strip())
    if not candidate.is_absolute():
        candidate = (base / candidate).resolve()
    else:
        candidate = candidate.resolve()

    if candidate == base or base in candidate.parents:
        return candidate
    return None
