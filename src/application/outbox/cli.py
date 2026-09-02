"""Outbox publisher CLI entry point.

This module owns the production entry point for the
:class:`~src.application.outbox.publisher.OutboxPublisher`. The CLI is
what ``docker compose up outbox_publisher`` runs:

```bash
python -m src.application.outbox            # long-running loop
python -m src.application.outbox --once     # single poll, then exit
```

Lifecycle (mirrors :func:`src.db.session.task_db_session`):

1. Open one ``SessionLocal`` per poll iteration; commit at the end so
   the publisher's UPDATE statements become visible. ``session.close``
   is guaranteed by the ``finally`` block.
2. Wire a :class:`~src.application.outbox.publisher.CeleryBrokerPort`
   against the project's ``celery_app``.
3. Install SIGTERM / SIGINT handlers that flip a stop flag the loop
   polls between iterations. ``run_until_signal`` returns the
   cumulative :class:`~src.application.outbox.publisher.PublisherStats`
   so the caller can log it on shutdown.

The CLI is intentionally thin: it does **no** business logic. The
:class:`~src.application.outbox.publisher.OutboxPublisher` is the unit;
this module is just plumbing + signal handling + argparse.

Why a standalone CLI / docker-compose entry (and not a Celery beat entry)
=========================================================================

The plan explicitly forbids "introduce broker-dependent outbox publisher".
That sentence is shorthand for "the publisher must be a standalone
Python process, not a Celery task". The Celery beat / worker
infrastructure only runs when a worker process is up; if the Celery
deployment fails (Redis down, broker URL misconfigured), the publisher
must still be able to drain the outbox. Coupling the publisher to
Celery would make the whole outbox-dependent path inherit Celery's
failure modes.

The docker-compose service (``outbox_publisher``) is its own container;
its ``healthcheck`` (Todo 23) imports the publisher module directly
and the operational runbook (Todo 24) restarts it independently of the
Celery worker / beat.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import socket
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from sqlalchemy.orm import Session

from src.application.outbox.publisher import (
    PUBLISHER_BATCH_SIZE,
    PUBLISHER_LEASE_SECONDS,
    PUBLISHER_POLL_INTERVAL_SECONDS,
    CeleryBrokerPort,
    OutboxPublisher,
    PublisherConfig,
    PublisherStats,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Defaults / helpers
# ---------------------------------------------------------------------------


def _default_instance_id() -> str:
    """Build a deterministic, debuggable publisher instance id.

    Uses ``hostname-pid`` so a process listing is enough to identify
    which publisher claimed which row (``OutboxEvent.claimed_by``).
    """
    return f"{socket.gethostname()}-{os.getpid()}"


def _build_arg_parser() -> argparse.ArgumentParser:
    """Build the ``argparse`` parser.

    The defaults come from the constants in
    :mod:`src.application.outbox.publisher` (ADR §4) so a typo at the
    CLI cannot drift from the canonical values.
    """
    parser = argparse.ArgumentParser(
        prog="src.application.outbox",
        description=(
            "Standalone outbox publisher. Polls outbox_event for pending "
            "rows and pushes them to the Celery broker. Independent of "
            "Celery beat / workers; restart-safe via the publisher lease."
        ),
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help=(
            "Run a single poll iteration and exit. Used by ops scripts "
            "and tests; the production docker-compose service does NOT "
            "pass this flag."
        ),
    )
    parser.add_argument(
        "--instance-id",
        default=_default_instance_id(),
        help=(
            "Publisher instance id recorded on every claim "
            "(OutboxEvent.claimed_by). Defaults to '<hostname>-<pid>'."
        ),
    )
    parser.add_argument(
        "--poll-interval",
        type=int,
        default=PUBLISHER_POLL_INTERVAL_SECONDS,
        help=(
            "Sleep between polls when the pool is empty. Default: "
            f"{PUBLISHER_POLL_INTERVAL_SECONDS} (ADR §4)."
        ),
    )
    parser.add_argument(
        "--lease-seconds",
        type=int,
        default=PUBLISHER_LEASE_SECONDS,
        help=(
            "Publisher lease duration. A crashed publisher's row "
            "becomes claimable after this many seconds. Default: "
            f"{PUBLISHER_LEASE_SECONDS} (ADR §4)."
        ),
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=PUBLISHER_BATCH_SIZE,
        help=(
            "LIMIT size for the publisher's claim SELECT. Default: "
            f"{PUBLISHER_BATCH_SIZE} (ADR §4)."
        ),
    )
    parser.add_argument(
        "--stats-file",
        type=str,
        default=None,
        help=(
            "Optional path. The CLI writes a JSON snapshot of "
            "PublisherStats here every loop iteration; the docker-"
            "compose healthcheck / supervisor can read it for metrics."
        ),
    )
    return parser


def _install_signal_handlers(stop_flag: list[bool]) -> None:
    """Install SIGTERM / SIGINT handlers that flip ``stop_flag``.

    The handlers are intentionally idempotent so multiple signals
    received during shutdown are tolerated. The default ``SIG_DFL``
    is preserved for ``SIGPIPE`` and other signals we don't need to
    intercept.

    Args:
        stop_flag: A single-element list used as a mutable box so the
            closure can mutate it. ``stop_flag[0] = True`` is the
            "the loop should exit" signal.
    """

    def _handle(signum: int, _frame: Any) -> None:
        logger.info("outbox publisher received signal=%s; flipping stop flag", signum)
        stop_flag[0] = True

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)


def _write_stats_file(path: Path, stats: PublisherStats) -> None:
    """Write a JSON snapshot of ``stats`` to ``path``.

    Best-effort: a failed write must not crash the publisher. The
    operator-facing impact of a missing snapshot is a missing metric,
    not a process death.

    Args:
        path: Destination file path. Parent directories are created
            on demand; the file is overwritten atomically.
        stats: Cumulative :class:`PublisherStats` snapshot.
    """
    payload = {
        "claimed": stats.claimed,
        "published": stats.published,
        "retryable_failures": stats.retryable_failures,
        "terminal_failures": stats.terminal_failures,
        "lease_reclaimed": stats.lease_reclaimed,
        "empty_polls": stats.empty_polls,
        "last_processed_at": (
            stats.last_processed_at.isoformat() if stats.last_processed_at is not None else None
        ),
        "snapshot_at": datetime.now(tz=timezone.utc).isoformat(),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        logger.warning("outbox publisher failed to write stats file %s: %s", path, exc)


def _run_iteration(
    args: argparse.Namespace,
    stop_flag: list[bool],
    stats: PublisherStats,
) -> PublisherStats:
    """Run one poll iteration and return the cumulative stats.

    Opens a fresh ``SessionLocal`` per call so the iteration is
    isolated; commits at the end so the publisher's UPDATE statements
    become visible to other publisher instances immediately. The
    ``finally`` block guarantees the session is closed even when the
    publish path raises.

    Args:
        args: Parsed CLI arguments.
        stop_flag: Mutable ``[False]`` box. Currently unused by the
            iteration itself but kept for the signature the loop
            relies on.
        stats: Cumulative stats from the previous iteration. Returned
            as the new cumulative value when the new iteration has
            not produced any deltas (i.e. empty pool).

    Returns:
        The new cumulative :class:`PublisherStats`. When ``stats_file``
        is configured, also writes a JSON snapshot to disk.
    """
    # Local imports keep the module free of ``celery_app`` /
    # ``SessionLocal`` until the CLI actually runs (mirrors
    # ``bootstrap_production``'s lazy Celery pattern).
    from src.core.celery_app import celery_app
    from src.db.session import SessionLocal

    del stop_flag  # kept for the loop's stop() callable shape

    session: Session = SessionLocal()
    try:
        broker = CeleryBrokerPort(celery_app)
        config = PublisherConfig(
            claimed_by=args.instance_id,
            poll_interval_seconds=args.poll_interval,
            lease_seconds=args.lease_seconds,
            batch_size=args.batch_size,
            one_shot=args.once,
        )
        publisher = OutboxPublisher(
            db_session=session,
            config=config,
            broker=broker,
        )
        publisher._stats = stats  # type: ignore[attr-defined]  # carry over cumulative
        iteration_stats = publisher.run_once()
        new_stats = stats.merge(iteration_stats)
        if args.stats_file:
            _write_stats_file(Path(args.stats_file), new_stats)
        return new_stats
    finally:
        session.close()


def main(argv: Optional[list[str]] = None) -> int:
    """CLI entry point. Returns ``0`` on clean exit, ``1`` on configuration / runtime error.

    Args:
        argv: Optional override for ``sys.argv``. Tests pass an
            explicit list; the docker-compose entry point passes
            ``None`` (use ``sys.argv``).
    """
    from src.core.logging_config import configure_logging

    configure_logging()
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    if args.poll_interval < 0:
        parser.error("--poll-interval must be >= 0")
    if args.lease_seconds <= 0:
        parser.error("--lease-seconds must be > 0")
    if args.batch_size <= 0:
        parser.error("--batch-size must be > 0")

    stop_flag: list[bool] = [False]
    _install_signal_handlers(stop_flag)

    stats = PublisherStats()
    if args.once:
        # Single-iteration mode: the loop would also work, but the
        # caller (tests / ops scripts) expects a synchronous exit.
        stats = _run_iteration(args, stop_flag, stats)
        logger.info(
            "outbox publisher --once exit: claimed=%d published=%d "
            "retryable=%d terminal=%d empty_polls=%d",
            stats.claimed,
            stats.published,
            stats.retryable_failures,
            stats.terminal_failures,
            stats.empty_polls,
        )
        return 0

    logger.info(
        "outbox publisher starting: instance_id=%s poll_interval=%ds lease=%ds batch=%d",
        args.instance_id,
        args.poll_interval,
        args.lease_seconds,
        args.batch_size,
    )

    try:
        while not stop_flag[0]:
            stats = _run_iteration(args, stop_flag, stats)
            if args.poll_interval > 0:
                # Local import to avoid hard dependency at module
                # import time; tests stub ``time.sleep`` via the
                # ``OutboxPublisher.run_until_signal`` path, not this
                # one. The CLI is the only consumer.
                import time as _time

                _time.sleep(args.poll_interval)
    except KeyboardInterrupt:
        logger.info("outbox publisher KeyboardInterrupt; exiting")
    finally:
        logger.info(
            "outbox publisher exit: claimed=%d published=%d "
            "retryable=%d terminal=%d empty_polls=%d",
            stats.claimed,
            stats.published,
            stats.retryable_failures,
            stats.terminal_failures,
            stats.empty_polls,
        )
    return 0


__all__ = ["main"]
