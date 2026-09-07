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
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

from src.application.pipeline.commands import SessionBuildCommand
from src.models.video_source import VideoSource
from src.services.pipeline_constants import ScanMode

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


def dispatch_hot_builds(db: Session) -> list[dict]:
    """Enqueue one HOT-mode session build per enabled, un-paused source.

    Reads the composition-root :class:`TaskDispatcherPort` so unit
    tests can swap in :class:`FakeTaskDispatcher` without
    monkeypatching. Per-source exceptions are logged and skipped so
    one bad source does not break the heartbeat.

    Returns the per-source dispatch envelope (``source_id`` +
    ``task_id``) so the heartbeat aggregator can report the exact
    dispatched count without re-querying the DB.
    """
    sources = (
        db.query(VideoSource)
        .filter(VideoSource.enabled.is_(True), VideoSource.source_paused.is_(False))
        .all()
    )
    from src.tasks._container import get_container

    dispatcher = get_container().dispatcher
    dispatched: list[dict] = []
    for source in sources:
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
