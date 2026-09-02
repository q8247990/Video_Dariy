"""Session builder facade.

This module is now a **thin façade** in front of the
:mod:`src.services.session_build` stage package. The legacy
``SessionBuilder.build(...)`` signature is preserved so the
existing unit tests in ``tests/unit/test_session_builder.py``
and ``tests/integration/test_source_scan_concurrency_postgres.py``
keep working without modification; the canonical entry point
for new code is :func:`src.services.session_build.runner.run`.

History
=======

* The pre-Wave-5 monolith in this file owned the entire
  pipeline: scan → dedupe → merge → seal, in one ``build``
  method with a 90-line God-Method body. Wave 5 split the body
  into five stages under :mod:`src.services.session_build`
  so each stage is independently testable, the pure reducer
  is DB-free, and the slim Celery task can drive the stages
  through :func:`src.services.session_build.runner.run`.

* The façade keeps :class:`SessionBuilder.build` so
  ``tests.unit.test_session_builder`` (8 cases) and
  ``tests.integration.test_source_scan_concurrency_postgres``
  (``test_full_and_hot_build_collision_preserves_one_file_and_relation``
  and the dispatcher / advisory-lock tests) keep their
  pre-Wave-5 setup intact. Existing tests
  ``monkeypatch-setattr`` on
  ``src.services.session_builder.XiaomiDirectoryParser.scan_directory``
  to short-circuit the parser; that monkeypatch now lands on
  :mod:`src.services.session_build.discovery`'s
  ``XiaomiDirectoryParser.scan_directory`` import (the
  discovery stage goes through the same class).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Callable
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from src.adapters.xiaomi_parser import (
    XiaomiDirectoryParser,  # noqa: F401 — re-exported for the legacy ``monkeypatch.setattr`` import-path used by ``tests/unit/test_session_builder.py``.
)
from src.services.session_build.runner import run
from src.services.session_build.types import SealedSessionInfo, SessionBuildResult

logger = logging.getLogger(__name__)


# Re-export the constants and helpers that the legacy
# ``session_builder`` module pinned at module scope; tests
# monkeypatch ``SessionBuilder._acquire_source_mutation_lock``
# to assert the lock is still called on the unit-test path.
MERGE_GAP_SECONDS = 1
SEAL_BUFFER_SECONDS = 600
HASH_QUERY_CHUNK_SIZE = 500


class SessionBuilder:
    """Thin façade preserving the legacy ``SessionBuilder.build`` signature.

    The implementation delegates to
    :func:`src.services.session_build.runner.run` so the
    pre-existing test suite (8 unit cases + the PG
    concurrency case) keeps passing unchanged. New code
    should call :func:`src.services.session_build.runner.run`
    directly — the façade is here only for backward
    compatibility.
    """

    def build(
        self,
        db: Session,
        source_id: int,
        root_path: str,
        scan_mode: str,
        scan_start: datetime,
        scan_end: datetime,
        cancel_check: Callable[[], None] | None = None,
        timezone: ZoneInfo | None = None,
    ) -> SessionBuildResult:
        self._acquire_source_mutation_lock(db, source_id)
        return run(
            db,
            source_id=source_id,
            root_path=root_path,
            scan_mode=scan_mode,
            scan_start=scan_start,
            scan_end=scan_end,
            cancel_check=cancel_check,
            home_zone=timezone,
        )

    # ------------------------------------------------------------------
    # Backward-compatible attribute surface
    # ------------------------------------------------------------------

    @staticmethod
    def _acquire_source_mutation_lock(db: Session, source_id: int) -> None:
        """Backward-compat no-op used by tests that monkeypatch this method.

        Production code now goes through
        :func:`src.services.session_build.runner.acquire_source_advisory_lock`;
        tests that used to call ``SessionBuilder._acquire_source_mutation_lock``
        continue to find it because the lock helper is owned by the
        runner module and re-exported here for convenience.
        """
        from src.services.session_build.runner import acquire_source_advisory_lock

        return acquire_source_advisory_lock(db, source_id)


__all__ = [
    "HASH_QUERY_CHUNK_SIZE",
    "MERGE_GAP_SECONDS",
    "SEAL_BUFFER_SECONDS",
    "SealedSessionInfo",
    "SessionBuilder",
    "SessionBuildResult",
]
