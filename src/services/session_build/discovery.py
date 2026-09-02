"""Discovery stage: walk a configured root path, parse files, sort by start_time.

The discovery stage is intentionally **read-only with respect to
the file system**. It receives a Xiaomi parser instance, calls
:func:`XiaomiDirectoryParser.scan_directory` inside the caller's
``[scan_start, scan_end]`` window, and returns the parsed records
sorted by ``start_time`` so the dedupe stage can iterate without
re-sorting. The parser's name-based folder pre-filter and
``cancel_check`` throttling are preserved verbatim.

This module owns the Xiaomi adapter integration; no other stage
imports :mod:`src.adapters.xiaomi_parser`.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from src.adapters.xiaomi_parser import XiaomiDirectoryParser
from src.services.session_build.types import DiscoveredFile

logger = logging.getLogger(__name__)


def discover_files(
    *,
    parser: XiaomiDirectoryParser,
    scan_start: datetime,
    scan_end: datetime,
    cancel_check: Optional[Callable[[], None]] = None,
) -> list[DiscoveredFile]:
    """Walk ``parser.root_path`` and return the sorted, parsed records.

    The discovery stage never inspects :attr:`XiaomiDirectoryParser.timezone`;
    the parser localises aware UTC internally, so the caller's
    bounds (``scan_start`` / ``scan_end``) must be aware UTC for
    the comparison in :func:`XiaomiDirectoryParser.scan_directory`
    to work. The slim Celery task in :mod:`src.tasks.session_build`
    enforces this by deriving both bounds from
    :func:`datetime.now(timezone.utc)`.

    Args:
        parser: The :class:`XiaomiDirectoryParser` to walk.
        scan_start: Inclusive lower bound for ``start_time``.
        scan_end: Inclusive upper bound for ``start_time``.
        cancel_check: Optional callable invoked from inside the
            parser's ``CANCEL_CHECK_INTERVAL`` throttling. Raises
            whatever the caller's framework (e.g.
            :class:`TaskCancellationRequested`) raises; discovery
            propagates it unchanged.

    Returns:
        Sorted list of :class:`DiscoveredFile`. The sort key is
        :attr:`DiscoveredFile.start_time` ascending so the dedupe
        stage can iterate in deterministic order.

    Raises:
        Whatever ``cancel_check`` raises; the parser aborts mid-loop
            and the records collected so far are still returned by the
            parser (callers that care about a clean cancel guard
            themselves with a follow-up ``ensure_task_not_cancelled``
            before the persistence step).
    """
    raw_records = parser.scan_directory(
        min_time=scan_start,
        max_time=scan_end,
        cancel_check=cancel_check,
    )
    raw_records.sort(key=lambda record: record["start_time"])
    return [DiscoveredFile.from_parser_record(record) for record in raw_records]


def build_parser(root_path: str, timezone: Optional[ZoneInfo] = None) -> XiaomiDirectoryParser:
    """Construct the parser for the hot/full runner.

    A thin helper so the parser import is owned by ``discovery``
    only (other stages should not need to import
    :class:`XiaomiDirectoryParser`).
    """
    return XiaomiDirectoryParser(root_path, timezone=timezone)


__all__ = ["build_parser", "discover_files"]
