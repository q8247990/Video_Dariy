"""Hot-scheduling policy — heartbeat-driven hot build dispatch.

The :func:`dispatch_hot_builds` helper runs every heartbeat tick:
walk every enabled, un-paused :class:`VideoSource` and enqueue a
HOT-mode session build via the composition-root
:class:`TaskDispatcherPort` (the production binding writes an
``OutboxEvent`` row; the fake binding records the dispatch).

The function is intentionally small — the heartbeat aggregator
calls it once per tick and reads the returned count off the
:func:`dispatch_hot_builds` payload to produce the deterministic
``hot_scans_dispatched`` counter that lands in the heartbeat result
dict. All per-source error handling is logged at the helper level so
a single failed source cannot poison the rest of the heartbeat.

The split from the pre-Wave-5 ``_dispatch_hot_builds`` keeps the
private prefix off the public surface (``heartbeat`` is in
``src.tasks.task_maintenance``; the policy module is the
implementation detail).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from src.application.pipeline.commands import SessionBuildCommand
from src.models.video_source import VideoSource
from src.services.pipeline_constants import ScanMode
from src.services.system_config_registry import SCAN_INTERVAL_SECONDS, get_config
from src.services.video_source_policy import is_source_schedulable

logger = logging.getLogger(__name__)


def _within_scan_interval(source: VideoSource, *, now: datetime, interval_seconds: int) -> bool:
    """True when the source was scanned more recently than the configured interval."""
    last_scan_at = source.last_scan_at
    if last_scan_at is None:
        return False
    if last_scan_at.tzinfo is None:
        last_scan_at = last_scan_at.replace(tzinfo=timezone.utc)
    return (now - last_scan_at).total_seconds() < interval_seconds


def dispatch_hot_builds(db: Session) -> list[dict]:
    """Enqueue one HOT-mode session build per schedulable source.

    A source is skipped when it is not schedulable
    (:func:`src.services.video_source_policy.is_source_schedulable`:
    disabled / paused / not validated / unsupported type) or when it was
    scanned more recently than ``scan_interval_seconds``. Reads the
    composition-root :class:`TaskDispatcherPort` so unit tests can swap in
    :class:`FakeTaskDispatcher`. Per-source exceptions are logged and
    skipped so one bad source does not break the heartbeat.

    Returns the per-source dispatch envelope (``source_id`` +
    ``task_id``) so the heartbeat aggregator can report the exact
    dispatched count without re-querying the DB.
    """
    sources = (
        db.query(VideoSource)
        .filter(VideoSource.enabled.is_(True), VideoSource.source_paused.is_(False))
        .all()
    )
    interval_seconds = int(get_config(db, SCAN_INTERVAL_SECONDS))
    now = datetime.now(timezone.utc)
    from src.tasks._container import get_container

    dispatcher = get_container().dispatcher
    dispatched: list[dict] = []
    for source in sources:
        schedulable, reason = is_source_schedulable(source)
        if not schedulable:
            logger.info("Skipping hot build for source %s: %s", source.id, reason)
            continue
        if _within_scan_interval(source, now=now, interval_seconds=interval_seconds):
            continue
        try:
            task_id = dispatcher.dispatch_session_build(
                db,
                SessionBuildCommand(source_id=source.id, scan_mode=ScanMode.HOT),
            )
            dispatched.append({"source_id": source.id, "task_id": task_id})
        except Exception:
            logger.exception("Failed to dispatch hot build for source %s", source.id)
    return dispatched


__all__ = ["dispatch_hot_builds"]
