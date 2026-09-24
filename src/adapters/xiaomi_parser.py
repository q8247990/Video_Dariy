import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

# cancel_check hits the database; checking every directory entry makes a scan
# of thousands of folders cost thousands of round trips.
CANCEL_CHECK_INTERVAL = 100


class XiaomiDirectoryParser:
    """
    Parses Xiaomi NAS backup directories.
    Expected structure: YYYYMMDDHH/MMSS_TIMESTAMP.mp4 or similar.
    We saw files like: xiaomi_video/2026022615/31M32S_1772091092.mp4
    Where 1772091092 is likely a unix timestamp or seconds since a specific epoch,
    Wait, let's look at 2026022615: Year 2026, Month 02, Day 26, Hour 15.
    31M32S: Minute 31, Second 32.
    So exact time is 2026-02-26 15:31:32.
    """

    def __init__(self, root_path: str, timezone: Optional[ZoneInfo] = None):
        self.root_path = root_path
        # Camera-local zone. When set, parsed naive camera times are localized
        # to aware UTC (safe to compare with UTC boundaries); None keeps the
        # legacy naive output.
        self.timezone = timezone

    def _to_utc(self, naive_time: datetime) -> datetime:
        if self.timezone is None:
            return naive_time
        return naive_time.replace(tzinfo=self.timezone).astimezone(timezone.utc)

    def parse_file_name(self, folder_name: str, file_name: str) -> Optional[dict]:
        """
        Parses folder and file name into start_time and estimated duration.
        Folder: YYYYMMDDHH (e.g., 2026022615)
        File: MM"M"SS"S_"*.mp4 (e.g., 31M32S_1772091092.mp4)
        """
        try:
            # Parse folder
            year = int(folder_name[0:4])
            month = int(folder_name[4:6])
            day = int(folder_name[6:8])
            hour = int(folder_name[8:10])

            # Parse file
            match = re.match(r"(\d+)M(\d+)S_", file_name)
            if not match:
                return None

            minute = int(match.group(1))
            second = int(match.group(2))

            start_time = self._to_utc(datetime(year, month, day, hour, minute, second))

            # Fallback estimate only: the filename carries no length. The
            # real duration is probed from the file at the dedupe/persistence
            # boundary; this 60s value is used when the file is absent or
            # ffprobe fails.
            duration = 60
            end_time = start_time + timedelta(seconds=duration)

            return {"start_time": start_time, "end_time": end_time, "duration_seconds": duration}
        except (IndexError, ValueError) as exc:
            logger.debug(
                "Failed to parse Xiaomi video filename (folder=%s, file=%s): %s",
                folder_name,
                file_name,
                exc,
            )
            return None

    def get_directory_time_bounds(self) -> tuple[Optional[datetime], Optional[datetime]]:
        if not os.path.exists(self.root_path):
            return None, None

        folder_times: list[datetime] = []
        for folder_name in os.listdir(self.root_path):
            if len(folder_name) != 10 or not folder_name.isdigit():
                continue
            try:
                folder_time = self._to_utc(datetime.strptime(folder_name, "%Y%m%d%H"))
            except ValueError:
                continue
            folder_path = os.path.join(self.root_path, folder_name)
            if not os.path.isdir(folder_path):
                continue
            folder_times.append(folder_time)

        if not folder_times:
            return None, None
        return min(folder_times), max(folder_times)

    def scan_directory(  # noqa: C901
        self,
        min_time: Optional[datetime] = None,
        max_time: Optional[datetime] = None,
        cancel_check: Optional[Callable[[], None]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Scan directory and return parsed video files.
        If min_time is provided, only return videos strictly after min_time.
        If max_time is provided, only return videos not later than max_time.
        """
        results: List[Dict[str, Any]] = []
        if not os.path.exists(self.root_path):
            return results

        iterations = 0
        for folder_name in os.listdir(self.root_path):
            iterations += 1
            if cancel_check is not None and iterations % CANCEL_CHECK_INTERVAL == 0:
                cancel_check()

            # Name-based pre-filter (pure CPU, no syscall) before touching
            # the filesystem: out-of-window folders are skipped without stat.
            if len(folder_name) == 10 and folder_name.isdigit():
                try:
                    folder_time = self._to_utc(datetime.strptime(folder_name, "%Y%m%d%H"))
                    # If folder is entirely older than min_time hour, skip
                    if min_time and folder_time + timedelta(hours=1) <= min_time:
                        continue
                    # If folder starts after max_time, skip
                    if max_time and folder_time > max_time:
                        continue
                except ValueError:
                    pass

            folder_path = os.path.join(self.root_path, folder_name)
            if not os.path.isdir(folder_path):
                continue

            for file_name in os.listdir(folder_path):
                iterations += 1
                if cancel_check is not None and iterations % CANCEL_CHECK_INTERVAL == 0:
                    cancel_check()
                if not file_name.endswith(".mp4"):
                    continue

                parsed = self.parse_file_name(folder_name, file_name)
                if not parsed:
                    continue

                start_time = parsed["start_time"]

                if min_time and start_time <= min_time:
                    continue
                if max_time and start_time > max_time:
                    continue

                file_path = os.path.join(folder_path, file_name)
                # Convert to absolute or relative depending on system,
                # storing relative to root_path or absolute is fine.

                results.append(
                    {
                        "file_name": file_name,
                        "file_path": file_path,
                        "start_time": start_time,
                        "end_time": parsed["end_time"],
                        "duration_seconds": parsed["duration_seconds"],
                        "file_size": os.path.getsize(file_path),
                        "file_format": "mp4",
                        "storage_type": "local_file",
                    }
                )

        return results
