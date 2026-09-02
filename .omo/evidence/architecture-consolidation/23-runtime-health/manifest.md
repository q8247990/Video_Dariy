# Todo 23 - Readiness, worker health, migration locks, deployment resource governance

Wave 6 hardens runtime health and ops: the backend gains liveness/readiness
probes, schema migration is serialized behind a PostgreSQL advisory lock so
concurrently starting containers never double-migrate, celery worker / vision
worker / beat / outbox publisher get docker-compose healthchecks, and both
compose files get CPU/memory limits plus log rotation. No schema migration is
added and no application behavior (analysis / summary / session-build /
dispatch / maintenance / outbox / summary-attempt / transition-log) is touched.

## Deliverables

1. **Liveness / readiness endpoints** (`src/main.py`)
   - `/health` unchanged (existing tests depend on it).
   - `GET /livez` - 200 whenever the process is up; no DB/Redis dependency.
   - `GET /readyz` - 200 ONLY when DB `SELECT 1` succeeds AND Redis PING
     succeeds AND the Alembic schema is at the expected head
     (`EXPECTED_ALEMBIC_REVISION = "20260902_0021"`); otherwise 503 with a
     JSON body naming each failing check.

2. **Alembic advisory lock** (`src/db/init_db.py`)
   - `run_migration_locked(migrator, lock_engine=None)` wraps `migrator()` in
     `pg_advisory_lock` / `pg_advisory_unlock` on `MIGRATION_ADVISORY_LOCK_KEY`,
     held on a dedicated connection for the whole migration, released in
     `finally`. Non-PostgreSQL dialects (SQLite unit tests) skip the lock.
   - `init_db` now calls `run_migration_locked(_run_alembic_upgrade_head)`,
     preserving the `DB_INIT_MAX_RETRIES` / `DB_INIT_RETRY_INTERVAL_SECONDS`
     retry behavior for "DB not up yet".

3. **Worker / beat / publisher health in docker-compose (both files)**
   - `celery_worker`, `celery_vision_worker`: /proc liveness probe matching a
     `celery ... worker` process (argv[0] filter avoids self-match).
   - `celery_beat`: /proc probe matching a `... beat` process.
   - `outbox_publisher`: /proc probe matching `src.application.outbox`
     (token split `'out'+'box'` to avoid checker self-match). Replaces the old
     import-only check with a genuine liveness probe.
   - `backend`: keeps its `/health` HTTP probe.
   - None of these touch the video mount or run ffmpeg.

4. **Deployment resource governance (both compose files)**
   - `deploy.resources.limits`: backend (2 cpu / 2g), celery_worker (2 cpu /
     2g), celery_vision_worker (2 cpu / 4g - deliberately generous, not
     over-capped for the single-concurrency vision/ffmpeg path).
   - Log rotation: `json-file` driver with `max-size` / `max-file` on every
     logger.
   - Network layering: single `app_bridge` bridge network unchanged and sound
     (documented in notepad decisions); video/data/HLS are host bind mounts,
     so resource/log settings cannot block them.

## Files

- `src/main.py` - `/livez`, `/readyz`; imports `src.db.readiness`
- `src/db/init_db.py` - `EXPECTED_ALEMBIC_REVISION`, `MIGRATION_ADVISORY_LOCK_KEY`,
  `run_migration_locked`; `init_db` uses it
- `src/db/readiness.py` - `check_database` / `check_redis` / `check_alembic_head`
  / `readiness_checks`
- `tests/unit/test_livez_readyz.py` - `/livez` 200; `/readyz` 200 all-OK;
  `/readyz` 503 when DB down (3 tests)
- `tests/integration/test_advisory_migration_lock_postgres.py` - concurrent
  bootstrap serializes and migrates exactly once; lock held then released
  (2 tests)
- `docker-compose.yml`, `docker-compose.release.yml` - healthchecks, resource
  limits, log rotation

## Verification (all green)

- `ruff check .` - passed
- `ruff format --check src tests` - passed (377 files)
- `mypy src` - passed (270 source files)
- `pytest tests/unit -q` - **640 passed** (637 + 3 new)
- `pytest -m postgres -q` - **98 passed** (96 + 2 new)
- `pytest tests/integration/test_postgres_smoke.py -q` - 4 passed (unchanged)
- `pytest tests/architecture -q` - 9 passed
- `alembic heads` - `20260902_0021` (single head; NO migration added)
- `docker compose config` - both dev and release parse

## Hashi / guardrails honored

- `/health`, `DEFAULT_ADMIN_PASSWORD`, MCP_TOKEN empty-allowed, CORS
  allow_origins: unchanged.
- Frozen modules (`outbox/`, `summary_attempt/`, `transition_log/`) and
  analyzer / summarizer / session_build / dispatch / maintenance stage modules:
  untouched.
- No migration added; no new dependencies; `frontend/` (incl. nginx.conf),
  `frontend/test-results/`, `.env`: untouched.

## Commit

Single commit: `ops: harden readiness migration and worker health`
