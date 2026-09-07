"""Regression: ON CONFLICT arbiter predicates must match the index under generic plans (psycopg3).

Background (2026-09-05 pipeline deadlock)
-----------------------------------------

The dispatch / worker-bind / outbox INSERTs target the partial unique
indexes with ``ON CONFLICT … WHERE`` arbiter predicates. When the
arbiter predicate was rendered with **bind parameters**, PostgreSQL
could not match it against the (constant) index predicate under a
*generic* plan and raised::

    InvalidColumnReference: there is no unique or exclusion constraint
    matching the ON CONFLICT specification

The trigger is driver-specific: **psycopg3** declares Python ``str``
parameters as ``text`` (OID 25) in the extended protocol, which coerces
the arbiter expression to a ``(status)::text IN(text, text)`` shape
that no longer structurally matches the index predicate
``(status)::text = ANY(text[])`` under a generic plan. **psycopg2**
leaves the parameter type undeclared, so its arbiter shape still
matches — which is exactly why the production bug was invisible to
every dev-machine test (the dev ``postgresql://`` URL selects
psycopg2, while production's ``postgresql+psycopg://`` URL selects
psycopg3).

The plan cache adopts a generic plan for a long-lived prepared
statement after roughly ten executions inside one transaction; the
production incident crossed that threshold inside the 775-iteration
stuck-session dispatch loop, aborting the transaction and deadlocking
the pipeline.

The fix renders the arbiter predicates as **literal** SQL constants
(``ACTIVE_DEDUPE_INDEX_PREDICATE`` / ``PENDING_INDEX_PREDICATE``),
which match the index under every plan mode and with every driver.

Test strategy
-------------

Tests run on an explicit **psycopg3** engine (the production driver)
with ``SET plan_cache_mode = force_generic_plan`` — pinning the exact
plan class that exposed the bug, deterministically, on the first
execution. A parameterized arbiter fails under this setup; a literal
arbiter succeeds.
"""

from __future__ import annotations

import uuid
from typing import Iterator

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from src.application.outbox import contracts as outbox_contracts
from src.application.outbox.contracts import OutboxCommand
from src.application.outbox.repository import OutboxRepository
from src.services.dispatch.claim import create_pending_task_log
from src.services.dispatch.worker_bind import bind_or_create_running_task_log
from src.services.pipeline_constants import TaskType
from tests.conftest import run_alembic_upgrade_head

# The pg3_db fixture depends on postgres_database_url, which is not in
# conftest's PG_MARKED_FIXTURES (only engine/session fixtures are), so
# mark the module explicitly for the skip/selection semantics.
pytestmark = pytest.mark.postgres


@pytest.fixture(autouse=True)
def _reset_emitted_event_ids() -> None:
    outbox_contracts._reset_emitted_event_ids_for_testing()


@pytest.fixture()
def pg3_db(postgres_database_url: str) -> Iterator[Session]:
    """Function-scoped session on a throwaway schema via the **psycopg3** driver.

    Mirrors the ``pg_db`` fixture (one-shot migrated schema, outer
    transaction rolled back at teardown) but forces the psycopg3
    driver — the production driver whose text-typed parameters break
    the parameterized arbiter under generic plans.
    """
    schema = f"vd_pg3_{uuid.uuid4().hex[:12]}"
    admin = create_engine(postgres_database_url)
    with admin.connect() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        conn.commit()
    admin.dispose()
    run_alembic_upgrade_head(postgres_database_url, schema)

    pg3_url = postgres_database_url.replace("postgresql://", "postgresql+psycopg://", 1)
    engine = create_engine(pg3_url, connect_args={"options": f"-csearch_path={schema}"})
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection)
    try:
        yield session
    finally:
        session.close()
        if transaction.is_active:
            transaction.rollback()
        connection.close()
        engine.dispose()
        cleanup = create_engine(postgres_database_url)
        with cleanup.connect() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            conn.commit()
        cleanup.dispose()


@pytest.fixture()
def generic_plan_mode(pg3_db: Session) -> Session:
    """Pin the session to generic plans — the plan class that broke the parameterized arbiter."""
    pg3_db.execute(text("SET plan_cache_mode = force_generic_plan"))
    try:
        yield pg3_db
    finally:
        try:
            pg3_db.execute(text("RESET plan_cache_mode"))
        except Exception:
            # Aborted transaction: the transaction-scoped GUC reverts with
            # the outer-transaction rollback the pg3_db fixture performs.
            pass


def _analysis_command() -> OutboxCommand:
    return OutboxCommand(
        task_name="src.tasks.analyzer.analyze_session_task",
        queue="analysis_hot",
        args=(1,),
        kwargs={},
    )


def test_claim_insert_matches_index_under_generic_plan(generic_plan_mode: Session) -> None:
    """create_pending_task_log (the incident path) under a forced generic plan + psycopg3."""
    for i in range(3):
        task_log, created = create_pending_task_log(
            generic_plan_mode,
            task_type=TaskType.SESSION_ANALYSIS.value,
            task_target_id=10_000_000 + i,
            detail_json={"scan_mode": "hot"},
        )
        assert created is True
        assert task_log.status == "pending"


def test_worker_bind_insert_matches_index_under_generic_plan(generic_plan_mode: Session) -> None:
    """bind_or_create_running_task_log fresh-INSERT branch (forced generic plan + psycopg3)."""
    for i in range(3):
        bound = bind_or_create_running_task_log(
            generic_plan_mode,
            queue_task_id=f"regression-queue-{i}",
            task_type=TaskType.SESSION_ANALYSIS.value,
            task_target_id=20_000_000 + i,
        )
        assert bound is not None
        assert bound.status == "running"


def test_outbox_enroll_matches_index_under_generic_plan(generic_plan_mode: Session) -> None:
    """Outbox enqueue INSERT (second dispatch-loop statement) — forced generic plan + psycopg3."""
    repository = OutboxRepository(generic_plan_mode)
    command = _analysis_command()
    for i in range(3):
        task_log, is_new = create_pending_task_log(
            generic_plan_mode,
            task_type=TaskType.SESSION_ANALYSIS.value,
            task_target_id=30_000_000 + i,
            detail_json={"scan_mode": "hot"},
        )
        assert is_new is True
        outcome = repository.enroll_with_task_log(task_log, command)
        assert outcome.created is True
