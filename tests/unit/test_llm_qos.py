"""Unit tests for :mod:`src.services.llm_qos` token accounting.

These tests run against the real PostgreSQL schema (``pg_db``) because the
idempotency contract is enforced by the ``uq_llm_usage_log_checkpoint``
unique constraint — the very constraint that produced the duplicate-key
failure when an already-successful checkpoint was re-run after its input
fingerprint changed.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from src.models.llm_provider import LLMProvider
from src.models.llm_usage_log import LLMUsageLog
from src.models.session_analysis_checkpoint import SessionAnalysisCheckpoint
from src.models.video_session import VideoSession
from src.models.video_source import VideoSource
from src.services.llm_qos import record_token_usage


def _seed_checkpoint(db: Session) -> tuple[int, int, int]:
    """Seed a provider, source, session and checkpoint; return their ids."""
    provider = LLMProvider(
        provider_name="qos-provider",
        provider_type="vision_provider",
        api_base_url="http://example.invalid/v1",
        api_key="",
        model_name="vision-test",
        timeout_seconds=30,
        enabled=True,
        supports_vision=True,
        is_default_vision=True,
    )
    db.add(provider)

    source = VideoSource(
        source_name="qos camera",
        camera_name="qos camera",
        location_name="home",
        source_type="local_directory",
        config_json={"root_path": "/tmp"},
        enabled=True,
    )
    db.add(source)
    db.flush()

    now = datetime.now(timezone.utc)
    session = VideoSession(
        source_id=source.id,
        session_start_time=now,
        session_end_time=now,
        analysis_status="sealed",
    )
    db.add(session)
    db.flush()

    checkpoint = SessionAnalysisCheckpoint(
        session_id=session.id,
        analysis_run_id="qos-run",
        chunk_index=0,
        sub_chunk_index=0,
        start_offset_seconds=0,
        input_fingerprint="fingerprint",
        state="success",
    )
    db.add(checkpoint)
    db.commit()
    return provider.id, session.id, checkpoint.id


def test_record_token_usage_replaces_row_for_same_checkpoint(pg_db: Session) -> None:
    """Two calls for one checkpoint leave ONE row holding the second values.

    A re-run after a fingerprint change resets the checkpoint and calls
    ``record_token_usage`` again with the same ``analysis_checkpoint_id``;
    the usage ledger must mirror the latest run instead of inserting a
    duplicate that trips ``uq_llm_usage_log_checkpoint``.
    """
    provider_id, session_id, checkpoint_id = _seed_checkpoint(pg_db)

    record_token_usage(
        pg_db,
        provider_id=provider_id,
        provider_name_snapshot="first-provider",
        scene="video_analysis",
        usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        session_id=session_id,
        analysis_checkpoint_id=checkpoint_id,
        now=datetime(2026, 3, 1, tzinfo=timezone.utc),
    )
    pg_db.commit()

    record_token_usage(
        pg_db,
        provider_id=provider_id,
        provider_name_snapshot="second-provider",
        scene="video_analysis_rerun",
        usage={"prompt_tokens": 20, "completion_tokens": 7, "total_tokens": 27},
        session_id=session_id,
        analysis_checkpoint_id=checkpoint_id,
        now=datetime(2026, 3, 2, tzinfo=timezone.utc),
    )
    pg_db.commit()

    rows = (
        pg_db.query(LLMUsageLog).filter(LLMUsageLog.analysis_checkpoint_id == checkpoint_id).all()
    )
    assert len(rows) == 1
    row = rows[0]
    assert row.session_id == session_id
    assert row.provider_id == provider_id
    assert row.provider_name_snapshot == "second-provider"
    assert row.scene == "video_analysis_rerun"
    assert row.usage_date.isoformat() == "2026-03-02"
    assert (row.prompt_tokens, row.completion_tokens, row.total_tokens) == (20, 7, 27)


def test_record_token_usage_without_checkpoint_still_appends_rows(pg_db: Session) -> None:
    """Checkpoint-less usage (QA, daily summary, ...) keeps the insert path.

    ``uq_llm_usage_log_checkpoint`` is UNIQUE on a nullable column, so
    multiple checkpoint-less rows are legal and must stay legal.
    """
    provider_id, session_id, _ = _seed_checkpoint(pg_db)

    for total in (15, 30):
        record_token_usage(
            pg_db,
            provider_id=provider_id,
            provider_name_snapshot="qa-provider",
            scene="qa_agent_loop",
            usage={
                "prompt_tokens": total - 5,
                "completion_tokens": 5,
                "total_tokens": total,
            },
            session_id=session_id,
        )
    pg_db.commit()

    rows = (
        pg_db.query(LLMUsageLog)
        .filter(LLMUsageLog.session_id == session_id)
        .order_by(LLMUsageLog.id)
        .all()
    )
    assert [row.total_tokens for row in rows] == [15, 30]
    assert all(row.analysis_checkpoint_id is None for row in rows)
