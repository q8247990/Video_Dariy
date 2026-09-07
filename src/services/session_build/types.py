"""Dataclasses shared across the session-build pipeline stages.

These types cross every stage boundary:

* :class:`DiscoveredFile` — the normalized scan output before
  hash dedupe (the discovery stage).
* :class:`InsertedFile` — a persisted :class:`VideoFile` row plus
  its computed file-path hash (the dedupe stage).
* :class:`SealedSessionInfo` / :class:`SessionBuildResult` — the
  hot/full runner return shape; ``SealedSessionInfo`` is what
  the analyzer dispatcher consumes (the slim Celery task
  iterates ``SessionBuildResult.sealed_sessions``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from src.models.video_file import VideoFile

# ---------------------------------------------------------------------------
# Discovery / dedupe
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DiscoveredFile:
    """One row produced by :class:`XiaomiDirectoryParser.scan_directory`.

    Camera-local aware times (``start_time`` / ``end_time``) are produced
    by the parser via :func:`XiaomiDirectoryParser._to_utc`; the
    persistence layer can compare against UTC boundaries without
    further normalization.
    """

    file_name: str
    file_path: str
    start_time: datetime
    end_time: datetime
    duration_seconds: int
    file_size: int
    file_format: str = "mp4"
    storage_type: str = "local_file"

    @classmethod
    def from_parser_record(cls, record: dict[str, Any]) -> "DiscoveredFile":
        """Hydrate from a Xiaomi-parser dict.

        The parser does not have type annotations and the test
        suite hand-builds dict literals; this helper is the
        single seam that keeps the stage modules type-safe.
        """
        return cls(
            file_name=str(record["file_name"]),
            file_path=str(record["file_path"]),
            start_time=record["start_time"],
            end_time=record["end_time"],
            duration_seconds=int(record["duration_seconds"]),
            file_size=int(record["file_size"]),
            file_format=str(record.get("file_format") or "mp4"),
            storage_type=str(record.get("storage_type") or "local_file"),
        )


@dataclass(frozen=True)
class InsertedFile:
    """A persisted :class:`VideoFile` row paired with its file-path hash.

    The :attr:`file_path_hash` is the canonical dedupe key the
    reducer and the existing-hash query both operate on; the
    row's :attr:`video_file.id` is what
    :class:`VideoSessionFileRel` points at.
    """

    video_file: VideoFile
    file_path_hash: str


# ---------------------------------------------------------------------------
# Runner return shape
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SealedSessionInfo:
    """One row of the analyzer-dispatch envelope.

    Consumed by the slim Celery task in
    :func:`src.tasks._session_build_orchestration._dispatch_analysis_for_sealed`,
    which loops over
    :attr:`SessionBuildResult.sealed_sessions` and calls the
    outbox dispatcher exactly once per row.
    """

    session_id: int
    source_id: int
    priority: str  # :class:`AnalysisPriority` value


@dataclass
class SessionBuildResult:
    """The whole-pipeline return shape.

    The hot/full runner in :func:`src.services.session_build.runner.run_hot`
    / :func:`run_full` populates every field; the slim Celery
    task serializes the result and iterates
    :attr:`sealed_sessions` for analyzer dispatch.
    """

    files_found: int = 0
    files_inserted: int = 0
    files_skipped: int = 0
    sessions_created: int = 0
    sessions_updated: int = 0
    sessions_sealed: int = 0
    sealed_sessions: list[SealedSessionInfo] = field(default_factory=list)


__all__ = [
    "DiscoveredFile",
    "InsertedFile",
    "SealedSessionInfo",
    "SessionBuildResult",
]
