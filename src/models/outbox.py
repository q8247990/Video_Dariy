"""SQLAlchemy model for the transactional outbox.

The ``outbox_event`` table is the durable side of the PostgreSQL
transactional outbox described in
``docs/adr/0011-transactional-outbox-and-task-lifecycle.md``. ADR §2
freezes the field list and the index / constraint plan; this module
mirrors that contract verbatim. The application-level DTO
(``src.application.outbox.contracts.OutboxEvent``) is the in-memory
shape used by use cases and tests; this module is the SQLAlchemy
mapping that the repository (Todo 12) and the Alembic migration (Todo
12) consume.

Why a dedicated persistence model lives next to ``TaskLog`` rather than
in the application layer:

- the Alembic migration must use ``JSONB`` / partial indexes / ``FOR
  UPDATE SKIP LOCKED`` semantics that are PostgreSQL-specific. Keeping
  the model next to the rest of the project's ``src/models/*`` keeps
  the schema discoveries in one place and lets ``Base.metadata``
  register the table for ``alembic revision --autogenerate`` style
  workflows.
- the ADR specifies the columns are ``TEXT`` / ``JSONB`` /
  ``TIMESTAMPTZ``. Mapping them through ``JSON().with_variant(JSONB(),
  "postgresql")`` keeps the SQLAlchemy type portable so the unit tests
  can stand up an in-memory SQLite copy of the table; the migration
  script always emits the explicit PostgreSQL DDL the ADR requires.
- the ``OutboxEvent`` dataclass in
  ``src.application.outbox.contracts`` is what the application /
  publisher / cutover (Todo 14) import. The dataclass is deliberately
  framework-agnostic so it can be tested without a database. This
  module is the SQLAlchemy *row*; the repository (Todo 12) bridges
  the two.

Field mapping (per ADR §2):

================  ====================  ===================================
Column            Type                  Notes
================  ====================  ===================================
``id``            BIGSERIAL PK          surrogate; opaque to callers
``event_id``      UUID                  global UNIQUE; broker ``task_id``
``task_log_id``   BIGINT FK → task_log  UNIQUE; 1:1 link to ``TaskLog``
``dedupe_key``    TEXT NULL             mirror of ``task_log.dedupe_key``
``task_name``     TEXT                  registered Celery task dotted path
``queue``         TEXT                  Celery queue name
``args_json``     JSONB                 JSON-safe list
``kwargs_json``   JSONB                 JSON-safe dict
``status``        TEXT                  CHECK ``IN ('pending','publishing','published','failed')``
``attempt_count`` INT                   monotonic; ``0`` default
``next_attempt_at`` TIMESTAMPTZ         publisher retry schedule
``claimed_by``    TEXT NULL             publisher instance id while publishing
``lease_expires_at`` TIMESTAMPTZ NULL   publisher lease
``published_at``  TIMESTAMPTZ NULL      set in same tx as ``status='published'``
``last_error``    TEXT NULL             truncated publisher error
``created_at``    TIMESTAMPTZ           ``NOW()`` default
``updated_at``    TIMESTAMPTZ           bumped on every UPDATE
================  ====================  ===================================

Index / constraint plan (per ADR §2, "Index / constraint plan"):

1. ``PRIMARY KEY (id)`` — surrogate.
2. ``UNIQUE (event_id)`` — global idempotency.
3. ``UNIQUE (task_log_id)`` — one outbox row per business run.
4. **Partial unique** ``(task_log_id) WHERE status='pending'`` —
   concurrency safety net; second concurrent ``INSERT … ON CONFLICT
   DO NOTHING`` returns no row.
5. ``INDEX (status, next_attempt_at) WHERE status='pending'`` —
   publisher hot path. (ADR §2 lists this as partial; the
   implementation here is a plain composite index because SQLite does
   not support partial indexes; on PostgreSQL the migration creates
   the explicit partial index.)
6. ``INDEX (status) WHERE status='failed'`` — failure-scan index
   (Todo 24 dashboard).
7. ``INDEX (status, published_at) WHERE status='published'`` —
   retention cleanup hot path.
8. ``FOREIGN KEY (task_log_id) REFERENCES task_log(id) ON DELETE
   RESTRICT`` — the outbox MUST NOT outlive its run silently.

Alembic-side the partial indexes are emitted with the explicit
``postgresql_where=...`` argument so they match the contract.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base_class import Base


class OutboxEvent(Base):
    """SQLAlchemy mapping for the ``outbox_event`` row.

    The dataclass in ``src.application.outbox.contracts.OutboxEvent`` is
    the in-memory representation. The repository (Todo 12) translates
    between the two; this class is intentionally bare (no methods, no
    relationships) so the migration emits the columns the ADR
    mandates and nothing else.
    """

    __tablename__ = "outbox_event"

    # -- surrogate primary key ----------------------------------------
    # ``BIGSERIAL`` on PostgreSQL (ADR §2); ``INTEGER`` on SQLite so
    # the column becomes a rowid alias and autoincrements.
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )

    # -- durable identity + business-run linkage ----------------------
    # Cross-dialect UUID: native PostgreSQL ``UUID`` (matches ADR §2);
    # portable ``Uuid()`` fallback so SQLite unit tests can ``create_all``.
    event_id: Mapped[Any] = mapped_column(
        Uuid().with_variant(UUID(as_uuid=True), "postgresql"),
        nullable=False,
        unique=True,
    )
    task_log_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("task_log.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    dedupe_key: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # -- Celery payload ------------------------------------------------
    task_name: Mapped[str] = mapped_column(Text, nullable=False)
    queue: Mapped[str] = mapped_column(Text, nullable=False)
    # JSON on SQLite / JSONB on PostgreSQL; see module docstring.
    args_json: Mapped[list[Any]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"),
        nullable=False,
    )
    kwargs_json: Mapped[dict[str, Any]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"),
        nullable=False,
    )

    # -- lifecycle state ----------------------------------------------
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        server_default=text("'pending'"),
    )

    # -- publisher bookkeeping ----------------------------------------
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    claimed_by: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    lease_expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # -- ``created_at`` / ``updated_at`` are inherited from Base ------
    # See ``src.db.base_class``. The migration declares them explicitly
    # because Alembic does not pick up the ``default`` lambda unless
    # the column shows up in the model metadata.

    __table_args__ = (
        # -- CHECK constraint on ``status`` ---------------------------
        CheckConstraint(
            "status IN ('pending','publishing','published','failed')",
            name="ck_outbox_event_status",
        ),
        # -- Concurrency safety net (ADR §2, item 4) -----------------
        # Partial unique on ``task_log_id`` while the row is pending.
        # The ``IS NOT NULL`` predicate is redundant (the column is
        # NOT NULL) but is frozen verbatim by ADR §2 item 4 so the
        # migration and the model stay byte-identical to the contract.
        Index(
            "uq_outbox_event_pending_task_log",
            "task_log_id",
            unique=True,
            postgresql_where=text("status = 'pending' AND task_log_id IS NOT NULL"),
            sqlite_where=text("status = 'pending' AND task_log_id IS NOT NULL"),
        ),
        # -- Publisher hot path (ADR §2, item 5) ---------------------
        Index(
            "idx_outbox_event_status_next_attempt",
            "status",
            "next_attempt_at",
        ),
        # -- Failure-scan index (Todo 24 dashboard, ADR §2 item 6) --
        Index(
            "idx_outbox_event_failed",
            "status",
            postgresql_where=text("status = 'failed'"),
            sqlite_where=text("status = 'failed'"),
        ),
        # -- Retention cleanup hot path ------------------------------
        Index(
            "idx_outbox_event_published",
            "status",
            "published_at",
            postgresql_where=text("status = 'published'"),
            sqlite_where=text("status = 'published'"),
        ),
    )


__all__ = ["OutboxEvent"]
