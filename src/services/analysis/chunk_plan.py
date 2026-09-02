"""Chunk planning for the analyzer pipeline.

Given a sealed :class:`VideoSession`, decide how it splits into
:class:`SessionVideoChunk` and :class:`SubChunk` work units and
compute the deterministic per-run fingerprint (``analysis_run_id``)
that anchors the checkpoint fencing contract.

The plan is deterministic given the same ``session_id`` / chunk
seconds / sub-chunk seconds inputs; that's the property the
checkpoint / finalize stages rely on to refuse late-worker writes
(``LateWorkerFencingError``). The fingerprint hashes the per-file
identity (path + size + mtime) of every video file that belongs to
the session, so a re-run on unchanged files is bit-identical.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from src.services.session_analysis_video import (
    SessionVideoChunk,
    SubChunk,
    build_chunk_sub_chunks,
    build_session_video_chunks,
)


@dataclass(frozen=True)
class SubChunkPlan:
    """A single sub-chunk work item — the unit the LLM sees once."""

    chunk_index: int
    sub_chunk_index: int
    start_offset_seconds: int
    duration_seconds: int
    file_paths: tuple[str, ...]

    def to_sub_chunk(self) -> SubChunk:
        return SubChunk(
            chunk_index=self.chunk_index,
            sub_chunk_index=self.sub_chunk_index,
            start_offset_seconds=self.start_offset_seconds,
            duration_seconds=self.duration_seconds,
            file_paths=list(self.file_paths),
        )


@dataclass(frozen=True)
class ChunkPlan:
    """The plan produced by :func:`build_chunk_plan` for one run."""

    session_id: int
    chunk_seconds: int
    sub_chunk_seconds: int
    chunks: tuple[SessionVideoChunk, ...]
    sub_chunks: tuple[SubChunkPlan, ...]
    analysis_run_id: str

    def sub_chunk_count(self) -> int:
        return len(self.sub_chunks)


def analysis_run_id_for_chunks(chunks: list[SessionVideoChunk]) -> str:
    """Stable per-run fingerprint hashed from the chunk file identities.

    Only path + size + mtime_ns participate; reading the file contents
    would be prohibitive and is unnecessary — every other layer
    (sub-chunk fingerprint, checkpoint writer, event-record replace)
    hashes a sha256 over the file paths themselves, so two different
    run_ids can only arise from genuinely different on-disk material.
    """
    identities: list[dict[str, Any]] = []
    for chunk in chunks:
        for path in chunk.file_paths:
            path_info: dict[str, Any] = {"path": path}
            if Path(path).exists():
                stat = Path(path).stat()
                path_info.update({"size": stat.st_size, "mtime_ns": stat.st_mtime_ns})
            identities.append(path_info)
    encoded = json.dumps(identities, sort_keys=True, separators=(",", ":")).encode()
    return sha256(encoded).hexdigest()


def build_chunk_plan(
    db: Session,
    *,
    session_id: int,
    chunk_seconds: int,
    sub_chunk_seconds: int,
) -> ChunkPlan:
    """Build the full (chunk → sub-chunk) work plan for a session.

    The DB session is used only for ``build_session_video_chunks`` /
    ``build_chunk_sub_chunks`` — neither call performs any external I/O
    beyond the per-file ``stat`` that ``analysis_run_id_for_chunks``
    itself does. The returned plan is fully materialised so callers
    can iterate over ``sub_chunks`` without holding a DB session.
    """
    chunks = build_session_video_chunks(db, session_id, chunk_seconds=chunk_seconds)
    run_id = analysis_run_id_for_chunks(chunks)
    sub_chunks: list[SubChunkPlan] = []
    for chunk in chunks:
        chunk_sub_chunks = build_chunk_sub_chunks(chunk, db, sub_chunk_seconds=sub_chunk_seconds)
        sub_chunks.extend(
            SubChunkPlan(
                chunk_index=sub.chunk_index,
                sub_chunk_index=sub.sub_chunk_index,
                start_offset_seconds=sub.start_offset_seconds,
                duration_seconds=sub.duration_seconds,
                file_paths=tuple(sub.file_paths),
            )
            for sub in chunk_sub_chunks
        )
    return ChunkPlan(
        session_id=session_id,
        chunk_seconds=chunk_seconds,
        sub_chunk_seconds=sub_chunk_seconds,
        chunks=tuple(chunks),
        sub_chunks=tuple(sub_chunks),
        analysis_run_id=run_id,
    )


def assemble_chunk_plan(
    *,
    session_id: int,
    chunk_seconds: int,
    sub_chunk_seconds: int,
    chunks: list[Any],
    chunk_sub_chunks_list: list[list[Any]],
) -> ChunkPlan:
    """Fold pre-built chunks / sub-chunks into a :class:`ChunkPlan`.

    Used by the slim analyzer task when the per-step
    ``build_session_video_chunks`` / ``build_chunk_sub_chunks`` calls
    are issued from the task's own namespace so unit-test
    ``monkeypatch.setattr("src.tasks.analyzer.X", ...)`` patches
    still apply. ``chunks[i]`` and ``chunk_sub_chunks_list[i]`` are
    paired 1:1.
    """
    run_id = analysis_run_id_for_chunks(chunks)
    sub_chunks: list[SubChunkPlan] = []
    for _chunk, chunk_sub_chunks in zip(chunks, chunk_sub_chunks_list, strict=True):
        for sub in chunk_sub_chunks:
            sub_chunks.append(
                SubChunkPlan(
                    chunk_index=sub.chunk_index,
                    sub_chunk_index=sub.sub_chunk_index,
                    start_offset_seconds=sub.start_offset_seconds,
                    duration_seconds=sub.duration_seconds,
                    file_paths=tuple(sub.file_paths),
                )
            )
    return ChunkPlan(
        session_id=session_id,
        chunk_seconds=chunk_seconds,
        sub_chunk_seconds=sub_chunk_seconds,
        chunks=tuple(chunks),
        sub_chunks=tuple(sub_chunks),
        analysis_run_id=run_id,
    )


def sub_chunk_fingerprint(
    *,
    system_prompt: str,
    user_prompt: str,
    video_data_url: str,
    sub_chunk: SubChunkPlan,
) -> str:
    """Per-sub-chunk content fingerprint used as a write condition.

    Combines the two prompt halves, the sub-chunk's start offset and
    duration, the exact list of files in the sub-chunk, and a sha256 of
    the raw ``data:video/mp4`` payload. Any of these changing is a
    signal the previous checkpoint was produced by an LLM call that no
    longer matches the current input and must be invalidated.
    """
    payload = {
        "system_prompt": system_prompt,
        "user_prompt": user_prompt,
        "video_sha256": sha256(video_data_url.encode()).hexdigest(),
        "start_offset_seconds": sub_chunk.start_offset_seconds,
        "duration_seconds": sub_chunk.duration_seconds,
        "file_paths": list(sub_chunk.file_paths),
    }
    return sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


__all__ = [
    "ChunkPlan",
    "SubChunkPlan",
    "analysis_run_id_for_chunks",
    "assemble_chunk_plan",
    "build_chunk_plan",
    "sub_chunk_fingerprint",
]
