"""Hash dedupe stage: skip existing files, insert new ones, mark missing.

This stage owns the :class:`VideoFile` persistence boundary. The
dedupe decision is a single point in the pipeline: every file the
discovery stage emits either already has a
:class:`VideoFile` row in the database (``files_skipped``) or
gets a fresh row here (``files_inserted``). The reducer stage
operates only on the inserted rows, so an existing file with a
matching hash is silently skipped before the reducer ever sees
it.

The per-build missing-file sweep was deliberately removed from
this stage in Wave 5: the dedicated per-file ``stat`` /
``file_missing`` flag mutation runs hourly from
:mod:`src.tasks.task_maintenance` instead. A per-minute build
does not need to ``stat`` the whole file history. The stage is
deliberately no-op when ``discovered_files`` is empty so the
runner does not regress the pre-Wave-5 behaviour.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.models.video_file import VideoFile, build_file_path_hash
from src.services.session_build.constants import HASH_QUERY_CHUNK_SIZE
from src.services.session_build.types import DiscoveredFile, InsertedFile

logger = logging.getLogger(__name__)


def compute_file_hashes(
    files: list[DiscoveredFile],
) -> list[tuple[DiscoveredFile, str]]:
    """Compute the file-path hash for every discovery row.

    A thin helper exposed for the rare test that wants to assert
    the dedupe key without writing anything.
    """
    return [(file, build_file_path_hash(file.file_path)) for file in files]


def query_existing_hashes(
    db: Session,
    source_id: int,
    hashes: list[str],
) -> set[str]:
    """Return the subset of ``hashes`` already present for ``source_id``.

    Implementation note: the existing PG deployment uses a single
    ``source_id`` equality filter and a chunked ``IN (...)`` over
    ``video_file.file_path_hash`` (the unique index is on
    ``(source_id, file_path_hash)``). The chunk size is bounded by
    SQLite's 999-parameter limit on the test engine; production PG
    has no such limit but the chunked loop keeps the helper
    symmetric across dialects.
    """
    existing: set[str] = set()
    distinct = sorted(set(hashes))
    for index in range(0, len(distinct), HASH_QUERY_CHUNK_SIZE):
        chunk = distinct[index : index + HASH_QUERY_CHUNK_SIZE]
        rows = (
            db.query(VideoFile.file_path_hash)
            .filter(
                VideoFile.source_id == source_id,
                VideoFile.file_path_hash.in_(chunk),
            )
            .all()
        )
        existing.update(str(row[0]) for row in rows)
    return existing


def insert_file(
    db: Session,
    source_id: int,
    file: DiscoveredFile,
    file_hash: str,
) -> Optional[InsertedFile]:
    """Insert a single :class:`VideoFile` row and return the inserted row.

    Returns ``None`` when the unique index
    ``uk_video_file_source_path_hash`` fires; the caller treats
    that as "already exists" and skips. Concurrent workers that
    race on the same hash see the same ``None`` so the
    reducer's append loop never adds two relations.

    The ``db.begin_nested()`` savepoint scopes the
    :class:`IntegrityError` so the outer transaction stays alive
    for the next iteration. Without the savepoint a rollback
    would nuke the whole batch and the reducer would never run.
    """
    try:
        with db.begin_nested():
            video_file = VideoFile(
                source_id=source_id,
                file_path_hash=file_hash,
                parse_status="parsed",
                file_name=file.file_name,
                file_path=file.file_path,
                start_time=file.start_time,
                end_time=file.end_time,
                duration_seconds=file.duration_seconds,
                file_size=file.file_size,
                file_format=file.file_format,
                storage_type=file.storage_type,
            )
            db.add(video_file)
            db.flush()
    except IntegrityError:
        return None
    return InsertedFile(video_file=video_file, file_path_hash=file_hash)


def dedupe_files(
    db: Session,
    *,
    source_id: int,
    discovered_files: list[DiscoveredFile],
    existing_hashes: Optional[set[str]] = None,
    cancel_check: Optional[Callable[[], None]] = None,
) -> list[InsertedFile]:
    """Return the post-dedupe :class:`InsertedFile` list.

    Pre-existing rows (those in ``existing_hashes``) and rows the
    helper just inserted (because two workers raced) are both
    excluded; the reducer sees exactly the rows its subsequent
    sessions will own.

    Args:
        db: Caller-owned SQLAlchemy session.
        source_id: The :class:`VideoSource.id` under scan.
        discovered_files: Sorted discovery output from
            :func:`src.services.session_build.discovery.discover_files`.
        existing_hashes: Optional pre-computed set of existing
            hashes (the runner takes a single batched query for
            efficiency). ``None`` triggers the in-stage query.
        cancel_check: Optional callable invoked before each insert
            so a long-running dedupe loop can be aborted mid-batch.

    Returns:
        The list of :class:`InsertedFile` ordered identically to
        :attr:`discovered_files`. Rows that already existed or
        lost the unique-index race are silently skipped; the
        caller can recover the count via
        ``len(discovered_files) - len(inserted)``.
    """
    if not discovered_files:
        return []

    with_hashes = compute_file_hashes(discovered_files)
    existing = (
        existing_hashes
        if existing_hashes is not None
        else query_existing_hashes(db, source_id, [h for _, h in with_hashes])
    )

    inserted: list[InsertedFile] = []
    for file, file_hash in with_hashes:
        if cancel_check is not None:
            cancel_check()
        if file_hash in existing:
            continue
        row = insert_file(db, source_id, file, file_hash)
        if row is None:
            existing.add(file_hash)
            continue
        existing.add(file_hash)
        inserted.append(row)
    return inserted


__all__ = [
    "compute_file_hashes",
    "dedupe_files",
    "insert_file",
    "query_existing_hashes",
]
