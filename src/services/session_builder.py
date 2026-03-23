"""Session builder service: scan files + merge into sessions in one pass.

Pure business logic, no Celery dependency. Reads video source directory
(read-only, never writes files) and produces/updates VideoSession records.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.adapters.xiaomi_parser import XiaomiDirectoryParser
from src.models.video_file import VideoFile, build_file_path_hash
from src.models.video_session import VideoSession
from src.models.video_session_file_rel import VideoSessionFileRel
from src.services.pipeline_constants import (
    AnalysisPriority,
    ScanMode,
    SessionAnalysisStatus,
)

logger = logging.getLogger(__name__)

MERGE_GAP_SECONDS = 61
SEAL_BUFFER_SECONDS = 600  # 10 minutes
HASH_QUERY_CHUNK_SIZE = 500


@dataclass
class SealedSessionInfo:
    session_id: int
    source_id: int
    priority: str  # AnalysisPriority.HOT | AnalysisPriority.FULL


@dataclass
class SessionBuildResult:
    files_found: int = 0
    files_inserted: int = 0
    files_skipped: int = 0
    sessions_created: int = 0
    sessions_updated: int = 0
    sessions_sealed: int = 0
    sealed_sessions: list[SealedSessionInfo] = field(default_factory=list)


class SessionBuilder:
    """Scans video files and merges them into sessions."""

    def build(  # noqa: C901
        self,
        db: Session,
        source_id: int,
        root_path: str,
        scan_mode: str,
        scan_start: datetime,
        scan_end: datetime,
        cancel_check: Callable[[], None] | None = None,
    ) -> SessionBuildResult:
        result = SessionBuildResult()
        priority = AnalysisPriority.HOT if scan_mode == ScanMode.HOT else AnalysisPriority.FULL

        if cancel_check is not None:
            cancel_check()

        # Step 1: Scan files (read-only)
        parser = XiaomiDirectoryParser(root_path)
        video_records = parser.scan_directory(
            min_time=scan_start,
            max_time=scan_end,
            cancel_check=cancel_check,
        )
        video_records.sort(key=lambda r: r["start_time"])
        result.files_found = len(video_records)

        if not video_records:
            # No new files; in hot mode check seal buffer for latest open session
            if scan_mode == ScanMode.HOT:
                sealed = self._seal_by_buffer(db, source_id, priority)
                result.sessions_sealed = len(sealed)
                result.sealed_sessions = sealed
            return result

        # Step 2: Deduplicate files by hash
        records_with_hash = [
            (record, build_file_path_hash(record["file_path"])) for record in video_records
        ]
        existing_hashes = self._query_existing_hashes(
            db, source_id, [h for _, h in records_with_hash]
        )

        # Step 3: Insert new files and merge into sessions
        current_session = self._get_latest_open_session(db, source_id)
        next_sort_index = (
            self._get_next_sort_index(db, current_session.id) if current_session else 0
        )

        for record, file_hash in records_with_hash:
            if cancel_check is not None:
                cancel_check()
            if file_hash in existing_hashes:
                result.files_skipped += 1
                continue

            # Insert file
            video_file = self._insert_file(db, source_id, record, file_hash)
            if video_file is None:
                result.files_skipped += 1
                existing_hashes.add(file_hash)
                continue

            existing_hashes.add(file_hash)
            result.files_inserted += 1

            # Merge into session
            if current_session is not None:
                gap = (video_file.start_time - current_session.session_end_time).total_seconds()
                if gap <= MERGE_GAP_SECONDS:
                    self._append_file_to_session(db, current_session, video_file, next_sort_index)
                    next_sort_index += 1
                    result.sessions_updated += 1
                    continue
                # Gap too large, current session is done
                current_session = None
                next_sort_index = 0

            # Create new session
            current_session = self._create_session(db, source_id, video_file)
            next_sort_index = 1
            result.sessions_created += 1

        # Step 4: Seal sessions
        if cancel_check is not None:
            cancel_check()
        sealed = self._seal_sessions(db, source_id, scan_mode, priority)
        result.sessions_sealed = len(sealed)
        result.sealed_sessions = sealed

        return result

    # ------------------------------------------------------------------
    # File operations
    # ------------------------------------------------------------------

    def _query_existing_hashes(
        self,
        db: Session,
        source_id: int,
        hashes: list[str],
    ) -> set[str]:
        existing: set[str] = set()
        distinct = sorted(set(hashes))
        for i in range(0, len(distinct), HASH_QUERY_CHUNK_SIZE):
            chunk = distinct[i : i + HASH_QUERY_CHUNK_SIZE]
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

    def _insert_file(
        self,
        db: Session,
        source_id: int,
        record: dict,
        file_hash: str,
    ) -> Optional[VideoFile]:
        video_file = VideoFile(
            source_id=source_id,
            file_path_hash=file_hash,
            parse_status="parsed",
            **record,
        )
        db.add(video_file)
        try:
            db.flush()
            return video_file
        except IntegrityError:
            db.rollback()
            return None

    # ------------------------------------------------------------------
    # Session operations
    # ------------------------------------------------------------------

    def _get_latest_open_session(self, db: Session, source_id: int) -> Optional[VideoSession]:
        return (
            db.query(VideoSession)
            .filter(
                VideoSession.source_id == source_id,
                VideoSession.analysis_status == SessionAnalysisStatus.OPEN,
            )
            .order_by(VideoSession.session_end_time.desc())
            .first()
        )

    def _get_next_sort_index(self, db: Session, session_id: int) -> int:
        latest_rel = (
            db.query(VideoSessionFileRel)
            .filter(VideoSessionFileRel.session_id == session_id)
            .order_by(VideoSessionFileRel.sort_index.desc())
            .first()
        )
        if not latest_rel:
            return 0
        return latest_rel.sort_index + 1

    def _create_session(
        self,
        db: Session,
        source_id: int,
        first_file: VideoFile,
    ) -> VideoSession:
        session = VideoSession(
            source_id=source_id,
            session_start_time=first_file.start_time,
            session_end_time=first_file.end_time,
            total_duration_seconds=first_file.duration_seconds or 0,
            analysis_status=SessionAnalysisStatus.OPEN,
        )
        db.add(session)
        db.flush()

        rel = VideoSessionFileRel(
            session_id=session.id,
            video_file_id=first_file.id,
            sort_index=0,
        )
        db.add(rel)
        return session

    def _append_file_to_session(
        self,
        db: Session,
        session: VideoSession,
        video_file: VideoFile,
        sort_index: int,
    ) -> None:
        rel = VideoSessionFileRel(
            session_id=session.id,
            video_file_id=video_file.id,
            sort_index=sort_index,
        )
        db.add(rel)

        session.session_end_time = max(session.session_end_time, video_file.end_time)
        if video_file.duration_seconds is not None:
            current = session.total_duration_seconds or 0
            session.total_duration_seconds = current + video_file.duration_seconds

    # ------------------------------------------------------------------
    # Seal logic
    # ------------------------------------------------------------------

    def _seal_sessions(
        self,
        db: Session,
        source_id: int,
        scan_mode: str,
        priority: str,
    ) -> list[SealedSessionInfo]:
        if scan_mode == ScanMode.FULL:
            return self._seal_all_open(db, source_id, priority)
        else:
            return self._seal_non_latest_open(db, source_id, priority)

    def _seal_all_open(
        self,
        db: Session,
        source_id: int,
        priority: str,
    ) -> list[SealedSessionInfo]:
        """Full mode: seal all open sessions (scan range is complete)."""
        open_sessions = (
            db.query(VideoSession)
            .filter(
                VideoSession.source_id == source_id,
                VideoSession.analysis_status == SessionAnalysisStatus.OPEN,
            )
            .order_by(VideoSession.session_start_time.asc())
            .all()
        )
        sealed: list[SealedSessionInfo] = []
        for session in open_sessions:
            session.analysis_status = SessionAnalysisStatus.SEALED
            session.analysis_priority = priority
            sealed.append(
                SealedSessionInfo(
                    session_id=session.id,
                    source_id=source_id,
                    priority=priority,
                )
            )
        if sealed:
            db.flush()
        return sealed

    def _seal_non_latest_open(
        self,
        db: Session,
        source_id: int,
        priority: str,
    ) -> list[SealedSessionInfo]:
        """Hot mode: seal all open sessions except the latest one.

        The latest open session stays open because new files may still arrive.
        If there are multiple open sessions, all but the latest are sealed
        (they have a subsequent session with gap > 61s, confirming completeness).
        """
        open_sessions = (
            db.query(VideoSession)
            .filter(
                VideoSession.source_id == source_id,
                VideoSession.analysis_status == SessionAnalysisStatus.OPEN,
            )
            .order_by(VideoSession.session_end_time.asc())
            .all()
        )
        if len(open_sessions) <= 1:
            return []

        # Seal all except the last (latest) one
        to_seal = open_sessions[:-1]
        sealed: list[SealedSessionInfo] = []
        for session in to_seal:
            session.analysis_status = SessionAnalysisStatus.SEALED
            session.analysis_priority = priority
            sealed.append(
                SealedSessionInfo(
                    session_id=session.id,
                    source_id=source_id,
                    priority=priority,
                )
            )
        if sealed:
            db.flush()

        # Also check seal buffer for the latest open session
        sealed.extend(self._seal_by_buffer(db, source_id, priority))
        return sealed

    def _seal_by_buffer(
        self,
        db: Session,
        source_id: int,
        priority: str,
    ) -> list[SealedSessionInfo]:
        """Fallback seal: if the latest open session's end_time is older than
        SEAL_BUFFER_SECONDS from now, seal it (no new files coming)."""
        cutoff = datetime.now() - timedelta(seconds=SEAL_BUFFER_SECONDS)
        stale_sessions = (
            db.query(VideoSession)
            .filter(
                VideoSession.source_id == source_id,
                VideoSession.analysis_status == SessionAnalysisStatus.OPEN,
                VideoSession.session_end_time <= cutoff,
            )
            .all()
        )
        sealed: list[SealedSessionInfo] = []
        for session in stale_sessions:
            session.analysis_status = SessionAnalysisStatus.SEALED
            session.analysis_priority = priority
            sealed.append(
                SealedSessionInfo(
                    session_id=session.id,
                    source_id=source_id,
                    priority=priority,
                )
            )
        if sealed:
            db.flush()
        return sealed
