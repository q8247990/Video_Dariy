"""Persistence helper for the ``daily_summary`` table.

A thin SQLAlchemy bridge for the publication use case (Todo 16). It
owns only the *upsert* the publish flow uses; reads, the legacy
``_upsert_daily_summary`` in :mod:`src.tasks.summarizer` (Todo 19) and
audit queries remain untouched.

Why a dedicated repository
==========================

The publish use case needs to write ``daily_summary`` atomically with
the attempt state transition (``→ succeeded``) and the outbox
enrollment (Todo 12 / Todo 14). The summarizer's existing
``_upsert_daily_summary`` is a free function that takes the model
fields individually; that shape is fine for the summarizer's own
caller, but it does not match the ``summary_content_json`` payload the
publish use case receives from the summarizer (Todo 19).

This repository is intentionally minimal — a single ``upsert_for_date``
method — so the publish use case has one obvious entry point. It uses
``INSERT … ON CONFLICT (summary_date) DO UPDATE`` on PostgreSQL and
SQLAlchemy's equivalent ``sqlite_insert`` on SQLite so the same code
runs against both dialects.

Conflict target
===============

The unique target is the ``summary_date`` ``UNIQUE`` constraint on
the model. A new generation for an already-published date is an
explicit overwrite (Todo 19 will only invoke the use case after a
successful generation), not a separate row — the audit history lives
in ``daily_summary_generation_attempt``, not in ``daily_summary``.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Optional

from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from src.models.daily_summary import DailySummary


class DailySummaryRepository:
    """SQLAlchemy bridge for ``daily_summary``.

    Instances are cheap — they hold only the ``Session``. Use one per
    task run / request / unit-test transaction; do not share across
    threads because ``Session`` is not thread-safe.
    """

    def __init__(self, db_session: Session) -> None:
        self._db = db_session

    # ------------------------------------------------------------------
    # Upsert (publish path)
    # ------------------------------------------------------------------

    def upsert_for_date(
        self,
        *,
        summary_date: date,
        summary_title: str,
        overall_summary: str,
        subject_sections_json: Optional[list[dict[str, Any]]],
        attention_items_json: Optional[list[dict[str, Any]]],
        event_count: int,
        provider_id: Optional[int],
        provider_name_snapshot: Optional[str],
    ) -> int:
        """Insert or update the ``daily_summary`` row for ``summary_date``.

        On PostgreSQL the insert uses
        ``INSERT … ON CONFLICT (summary_date) DO UPDATE … RETURNING id``;
        on SQLite the same shape is emitted via
        :func:`sqlalchemy.dialects.sqlite.insert`. The row is added to
        the caller's session but **not** committed; the caller owns
        the transaction so the upsert, the attempt state transition
        and the outbox enrollment share one commit.

        Args:
            summary_date: The date the summary covers.
            summary_title: Caller-supplied title (already localized).
            overall_summary: The prose body for the day.
            subject_sections_json: Per-subject roll-up; ``None`` when
                the summarizer had no events to map.
            attention_items_json: Operator-facing attention list.
            event_count: ``len(events)`` for the day; ``0`` for
                the empty-day fallback path.
            provider_id: The provider that produced the text; may be
                ``None`` when the caller short-circuited the LLM.
            provider_name_snapshot: The provider's display name at
                generation time; preserved separately from the FK
                because providers may be deleted.

        Returns:
            The ``id`` of the upserted ``daily_summary`` row.
        """
        now = datetime.now(tz=timezone.utc)
        values: dict[str, Any] = {
            "summary_date": summary_date,
            "summary_title": summary_title,
            "overall_summary": overall_summary,
            "subject_sections_json": subject_sections_json,
            "attention_items_json": attention_items_json,
            "event_count": event_count,
            "provider_id": provider_id,
            "provider_name_snapshot": provider_name_snapshot,
            "generated_at": now,
        }
        update_set: dict[str, Any] = {
            "summary_title": values["summary_title"],
            "overall_summary": values["overall_summary"],
            "subject_sections_json": values["subject_sections_json"],
            "attention_items_json": values["attention_items_json"],
            "event_count": values["event_count"],
            "provider_id": values["provider_id"],
            "provider_name_snapshot": values["provider_name_snapshot"],
            "generated_at": values["generated_at"],
        }

        if self._is_postgres():
            stmt = (
                postgresql_insert(DailySummary)
                .values(**values)
                .on_conflict_do_update(
                    index_elements=[DailySummary.summary_date],
                    set_=update_set,
                )
                .returning(DailySummary.id)
            )
            summary_id = self._db.execute(stmt).scalar_one()
            self._db.flush()
            return int(summary_id)

        # SQLite: use the dialect-specific insert with ON CONFLICT.
        # ``sqlite_insert`` has supported ``on_conflict_do_update``
        # targeting a UNIQUE column since SQLAlchemy 1.4; the
        # ``index_elements`` argument accepts the column directly
        # here because there is no partial-index distinction.
        insert_stmt: Any = sqlite_insert(DailySummary).values(**values)
        insert_stmt = insert_stmt.on_conflict_do_update(
            index_elements=[DailySummary.summary_date],
            set_=update_set,
        )
        self._db.execute(insert_stmt)
        self._db.flush()
        # SQLite path does not use RETURNING; re-read by the unique
        # column so the caller gets the canonical id.
        row = self._db.query(DailySummary).filter(DailySummary.summary_date == summary_date).one()
        return int(row.id)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _is_postgres(self) -> bool:
        bind = self._db.bind
        return bool(bind is not None and bind.dialect.name == "postgresql")


__all__ = ["DailySummaryRepository"]
