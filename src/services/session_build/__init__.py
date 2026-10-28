"""Session-build stages.

``types``
    Value objects and tunables shared by the scanner.
``discovery``
    Parse the camera layout and insert unseen files.
``reducer``
    Pure merge plan: which files join which session.
``persistence``
    Apply the reducer plan to the database.
``runner``
    Sealing policy plus the shared hot/full entry point.
"""

from __future__ import annotations

from src.services.session_build.discovery import (
    DiscoveredFile,
    InsertedFile,
    build_parser,
    dedupe_files,
    discover_files,
)
from src.services.session_build.persistence import (
    apply_file_actions,
    find_open_sessions_for_source,
    find_stuck_sealed_sessions,
)
from src.services.session_build.reducer import EXTEND, NEW_SESSION, ReducerPlan, reduce_files
from src.services.session_build.runner import (
    acquire_source_advisory_lock,
    apply_seal_transitions,
    run,
    run_full,
    run_hot,
    select_sessions_to_seal,
)
from src.services.session_build.types import (
    HASH_QUERY_CHUNK_SIZE,
    MERGE_GAP_SECONDS,
    SEAL_BUFFER_SECONDS,
    SealedSessionInfo,
    SessionBuildResult,
)

__all__ = [
    "EXTEND",
    "HASH_QUERY_CHUNK_SIZE",
    "MERGE_GAP_SECONDS",
    "NEW_SESSION",
    "SEAL_BUFFER_SECONDS",
    "DiscoveredFile",
    "InsertedFile",
    "ReducerPlan",
    "SealedSessionInfo",
    "SessionBuildResult",
    "acquire_source_advisory_lock",
    "apply_file_actions",
    "apply_seal_transitions",
    "build_parser",
    "dedupe_files",
    "discover_files",
    "find_open_sessions_for_source",
    "find_stuck_sealed_sessions",
    "reduce_files",
    "run",
    "run_full",
    "run_hot",
    "select_sessions_to_seal",
]
