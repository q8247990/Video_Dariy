"""Daily-summary publication use case.

Owns the atomic "persist the daily summary + finalise the attempt +
enroll webhook outbox rows" transaction that the summarizer (Todo 19)
calls once it has produced a successful generation for a date.

See :mod:`src.application.summary_publication.use_case` for the
algorithm and :mod:`src.application.summary_publication.repository`
for the persistence helper.

Atomicity contract
==================

All three writes happen in the **caller's** transaction. The caller
owns the commit; a ``rollback()`` between any two writes leaves zero
rows behind (ADR
``docs/adr/0011-transactional-outbox-and-task-lifecycle.md`` §1).
The publisher (Todo 13) picks the outbox rows up from the durable
table and publishes them to the broker — that is a separate
transaction.
"""

from __future__ import annotations

from src.application.summary_publication.repository import DailySummaryRepository
from src.application.summary_publication.use_case import (
    DAILY_SUMMARY_WEBHOOK_EVENT_TYPE,
    EMPTY_DAY_FALLBACK_TEXT,
    AttemptNotInValidStateError,
    PublishDailySummaryCommand,
    PublishDailySummaryOutcome,
    publish_daily_summary,
)

__all__ = [
    "DAILY_SUMMARY_WEBHOOK_EVENT_TYPE",
    "EMPTY_DAY_FALLBACK_TEXT",
    "AttemptNotInValidStateError",
    "DailySummaryRepository",
    "PublishDailySummaryCommand",
    "PublishDailySummaryOutcome",
    "publish_daily_summary",
]
