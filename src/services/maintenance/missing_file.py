"""Missing-file reconciliation — sweep enabled sources for vanished recordings.

The :func:`mark_missing_video_files` policy runs **once per hour** from
the heartbeat aggregator (when ``now.minute == 0``): walk every
enabled, un-paused :class:`VideoSource` and stamp ``file_missing=True``
/ ``missing_at=now`` on every :class:`VideoFile` whose path is no
longer reachable on disk.

The hourly cadence is load-bearing: the missing-file sweep stats
every known file (it has to — there is no incremental signal of
"file disappeared"). A per-minute cadence would be too expensive on
large catalogs. The heartbeat aggregator is the only thing that
decides the cadence.

The :func:`src.services.session_video.mark_missing_source_video_files`
helper does the per-source work; this module owns the loop that
walks the enabled sources and aggregates the deterministic count.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from src.models.video_source import VideoSource
from src.services.pipeline_constants import SourceType
from src.services.session_video import mark_missing_source_video_files

logger = logging.getLogger(__name__)


def mark_missing_video_files(db: Session) -> int:
    """Sweep every enabled, un-paused ``local_directory`` source.

    Returns the deterministic count of ``VideoFile`` rows that flipped
    from ``file_missing=False`` to ``file_missing=True`` in this call
    (the heartbeat aggregator reports it as ``missing_marked``).
    """
    sources = (
        db.query(VideoSource)
        .filter(VideoSource.enabled.is_(True), VideoSource.source_paused.is_(False))
        .all()
    )
    marked = 0
    for source in sources:
        if source.source_type != SourceType.LOCAL_DIRECTORY:
            continue
        marked += mark_missing_source_video_files(db, source.id)
    return marked


__all__ = ["mark_missing_video_files"]
