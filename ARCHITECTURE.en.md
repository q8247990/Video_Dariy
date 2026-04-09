> **中文版**: [ARCHITECTURE.md](ARCHITECTURE.md)

# Architecture Document

## 1. Purpose

This document describes the system architecture, module responsibilities, core business workflows, key data models, and deployment topology based on the current codebase. It serves as a reference for maintenance, delivery, and further development.

## 2. System Overview

This is an offline analysis system for home surveillance video.

Input consists of surveillance recording files from a NAS or local directory. Output includes structured events, playable sessions, daily family digests, and query capabilities for the frontend, bots, and agents.

The project is not a real-time stream processing system. It is an offline pipeline built around "directory scanning + asynchronous analysis".

## 3. High-Level Architecture

```text
                +----------------------+
                |   React + Nginx UI   |
                +----------+-----------+
                           |
                           v
                +----------------------+
                |   FastAPI Backend     |
                |  REST API / Media /   |
                |       MCP Server      |
                +----+-------------+----+
                     |             |
                     |             v
                     |     +---------------+
                     |     | OpenAI Compat  |
                     |     | LLM Providers  |
                     |     +---------------+
                     |
                     v
             +-------------------+
             | PostgreSQL        |
             | models + logs     |
             +-------------------+
                     ^
                     |
             +-------------------+
             | Celery Worker     |
             | build/analyze/    |
             | summary/webhook   |
             +-------------------+
                     ^
                     |
             +-------------------+
             | Redis             |
             | broker/backend    |
             +-------------------+

Video Directory / NAS -> Scan Task -> VideoFile -> VideoSession -> EventRecord -> DailySummary
```

## 4. Layers and Module Responsibilities

### 4.1 `src/main.py`

- Creates the FastAPI application
- Registers the API Router and MCP Router
- Runs `init_db()` at startup, which executes Alembic migrations and initializes the default admin user
- Exposes `/health` and `/health/bootstrap`

### 4.2 `src/api/`

Responsibility: External HTTP interface layer.

Key files:

- `src/api/v1/api.py`
- `src/api/v1/endpoints/auth.py`
- `src/api/v1/endpoints/video_sources.py`
- `src/api/v1/endpoints/tasks.py`
- `src/api/v1/endpoints/events.py`
- `src/api/v1/endpoints/sessions.py`
- `src/api/v1/endpoints/daily_summaries.py`
- `src/api/v1/endpoints/chat.py`
- `src/api/v1/endpoints/llm_providers.py`
- `src/api/v1/endpoints/webhooks.py`
- `src/api/v1/endpoints/media.py`
- `src/api/v1/endpoints/home_profile.py`
- `src/api/v1/endpoints/system_config.py`
- `src/api/v1/endpoints/onboarding.py`

Interface characteristics:

- Unified JWT authentication; dependencies defined in `src/api/deps.py`
- Provides paginated lists, detail views, create/update operations, and task triggers
- `media.py` serves raw file streams, session merged streams, and HLS playback manifests

### 4.3 `src/application/`

Responsibility: Application orchestration layer.

Sub-modules:

- `application/pipeline/`
  - Defines command objects and orchestrators
  - `PipelineOrchestrator` dispatches build, analysis, daily report, and webhook tasks
- `application/qa/`
  - Q&A use cases, retrieval strategies, evidence compression, output DTOs
- `application/mcp/`
  - Service wrappers for MCP tool invocations
- `application/prompt/`
  - Prompt compilation and context contract objects

Characteristics:

- This layer hosts "business orchestration logic" but is not a full DDD implementation; some core business rules still reside in `services`
- `src/application/bootstrap.py` currently serves as a compatibility placeholder and has not evolved into a true DI entry point

### 4.4 `src/services/`

Responsibility: Core domain services.

Key services:

- `session_builder.py`: Directory scanning, deduplication, session merging, sealing
- `session_analysis_video.py`: Session slicing, video segment processing
- `daily_summary/*`: Daily report preprocessing and output parsing
- `home_profile.py`: Family profile context building
- `dashboard.py`: Dashboard statistics
- `provider_selector.py`: Vision model / QA model selection
- `video_source_validator.py`: Video directory validation
- `webhook_payload.py` / `webhook_subscription.py`: Webhook event payloads and subscription matching
- `task_dispatch_control.py`: Task deduplication, running-state protection, task log binding

This is the most important layer in the current project, housing the majority of real business rules.

### 4.5 `src/tasks/`

Responsibility: Celery async execution layer.

Key tasks:

- `session_build.py`
  - Hot scan, full scan
- `analyzer.py`
  - Session visual analysis
- `summarizer.py`
  - Daily family report generation and scheduled dispatch
- `webhook.py`
  - Async webhook delivery
- `task_maintenance.py`
  - Heartbeat, timeout recovery, log cleanup

Scheduling strategy:

- `heartbeat`: Runs every 60 seconds
- `dispatch_scheduled_daily_summary_task`: Runs every 60 seconds
- Analysis tasks enter `analysis_hot` / `analysis_full` queues by priority

### 4.6 `src/models/`

Responsibility: Database model layer.

Core entities:

- `AdminUser`: Admin account
- `VideoSource`: Video source configuration
- `VideoFile`: Scanned recording file
- `VideoSession`: Logical session formed by merging consecutive recording clips
- `VideoSessionFileRel`: Ordered relationship between sessions and file segments
- `EventRecord`: Structured event identified by AI
- `DailySummary`: Daily report
- `LLMProvider`: Model service configuration
- `TaskLog`: Async task log and status
- `WebhookConfig`: Webhook configuration
- `HomeProfile` / `HomeEntityProfile`: Family profile
- `SystemConfig`: System-level configuration
- `AppRuntimeState`: Runtime protection state, e.g., daily report dispatch guard
- `ChatQueryLog`: Q&A query log
- `McpCallLog`: MCP call log

### 4.7 `src/db/`

Responsibility: Database infrastructure.

- `session.py`: Engine / SessionLocal / get_db
- `init_db.py`: Alembic upgrade, default admin initialization, database availability retry
- `base.py`: Model registration

Migration conventions:

- `20260320_0001` is the current PostgreSQL baseline snapshot
- Subsequent schema changes evolve through incremental Alembic revisions; runtime `create_all()` is no longer used as a migration mechanism

### 4.8 `src/providers/` and `src/infrastructure/`

Responsibility: External system adapters.

- `providers/openai_client.py`: OpenAI-compatible API client
- `infrastructure/llm/openai_gateway.py`: LLM Gateway factory and adapter
- `infrastructure/tasks/celery_dispatcher.py`: Task dispatcher implementation

### 4.9 `src/mcp/`

Responsibility: MCP Server and tool capabilities.

Key capabilities:

- Protocol version negotiation
- Token authentication
- Session management
- Tool listing and invocation
- MCP call logging

Implemented tools:

- `get_daily_summary`
- `search_events`
- `get_event_detail`
- `get_video_segments`
- `ask_home_monitor`

### 4.10 `frontend/`

Responsibility: Frontend admin console.

Tech stack: React 19, TypeScript, Vite, React Query, Zustand, hls.js.

Main pages:

- Login
- Dashboard
- Video source management
- Session list
- Event list and detail
- Daily report list
- Task center
- LLM Provider configuration
- Webhook configuration
- Family profile
- System configuration
- Chat Q&A
- Onboarding wizard

Nginx forwards `/api/`, `/mcp`, and `/health` to the backend.

## 5. Core Business Workflows

### 5.1 Video Source Scanning and Session Building

Core code:

- `src/tasks/session_build.py`
- `src/services/session_builder.py`
- `src/adapters/xiaomi_parser.py`

Workflow:

1. `task_maintenance.heartbeat` iterates over all enabled, non-paused video sources every 60 seconds
2. If no active scan task of the same type exists for a video source, a hot scan task is dispatched
3. `SessionBuilder.build()` calls `XiaomiDirectoryParser.scan_directory()` to scan the directory
4. `VideoFile.file_path_hash` is used for deduplication, preventing duplicate ingestion and analysis
5. New files are appended to the current open session in chronological order; if the gap between adjacent clips exceeds 61 seconds, a new session is created
6. In hot scan mode, only the most recent open session is kept; older open sessions are sealed
7. If an open session receives no new clips within 600 seconds, it is also sealed
8. After sealing, an analysis task is automatically dispatched

Key rules:

- Merge threshold: `MERGE_GAP_SECONDS = 61`
- Seal buffer: `SEAL_BUFFER_SECONDS = 600`
- Scan modes: `hot` / `full`

### 5.2 Session Analysis and Event Generation

Core code:

- `src/tasks/analyzer.py`
- `src/services/session_analysis_video.py`
- `src/services/video_analysis/output_parser.py`
- `src/services/video_analysis/mapper.py`

Workflow:

1. Tasks can only claim a session from `SEALED` state, transitioning it to `ANALYZING`
2. The session is sliced by `ANALYZER_SEGMENT_SECONDS`, defaulting to 600 seconds
3. For each slice, a video data URL and recognition prompt are constructed
4. An OpenAI-compatible vision model is called
5. The returned JSON is parsed and converted into multiple `EventRecord` entries
6. Previous events for the session are replaced (overwrite strategy)
7. Segment summaries are aggregated and written back to `VideoSession`
8. Analysis status is updated to `SUCCESS`

Additional mechanisms:

- Task log binding and status persistence
- Token quota checking and token usage recording
- Deadlock retry and analysis status rollback
- On failure, the raw model response summary fragments are preserved for debugging

### 5.3 Daily Family Report Generation

Core code:

- `src/tasks/summarizer.py`
- `src/services/daily_summary/preprocess.py`
- `src/services/daily_summary/output_parser.py`
- `src/application/prompt/compiler.py`

Workflow:

1. Beat checks every 60 seconds whether it is time to generate the daily report
2. By default, the report covers "yesterday's" data
3. Events for the target day are queried from `EventRecord`
4. Family profile data is combined to extract known subjects, topic mappings, and attention item candidates
5. If the prompt size is small, `single_pass` mode is used
6. If the prompt size is large, `split_serial` mode is used: summaries are first generated per subject, then an overall summary is produced
7. Results are trimmed to a display-friendly length range
8. Upsert is performed using `summary_date` as the unique key
9. If a relevant webhook subscription exists, a `daily_summary_generated` event is dispatched

Protection mechanisms:

- Dispatch guard in `AppRuntimeState` prevents duplicate dispatch within the same minute
- No regeneration occurs if a summary already exists or a task is already running

### 5.4 Natural Language Q&A

Core code:

- `src/api/v1/endpoints/chat.py`
- `src/application/qa/service.py`
- `src/application/qa/planner.py`
- `src/application/qa/retriever.py`
- `src/application/qa/evidence_compressor.py`

Workflow:

1. Receives a question text
2. Selects a QA Provider
3. Builds family context
4. Uses the LLM to produce a QueryPlan / RetrievalPlan
5. Performs layered retrieval across daily reports, sessions, and events
6. Compresses evidence to reduce context overhead
7. Calls the model again to generate the final answer
8. Records a `ChatQueryLog`

Characteristics:

- Not a simple full-text search; follows a "understand question → plan retrieval → organize evidence → answer" pipeline
- Can return referenced events and sessions

### 5.5 Webhook Push

Core code:

- `src/api/v1/endpoints/webhooks.py`
- `src/tasks/webhook.py`
- `src/services/webhook_payload.py`
- `src/services/webhook_subscription.py`

Workflow:

1. Users configure webhook URLs, enable status, and subscribed events in the admin console
2. The system constructs standardized payloads at event trigger points
3. Celery tasks deliver HTTP POST requests asynchronously
4. Subscription matching logic supports standardized `event_subscriptions_json`

### 5.6 MCP Capabilities

Core code:

- `src/mcp/server.py`
- `src/mcp/tools.py`
- `src/application/mcp/service.py`

Characteristics:

- Uses a JSON-RPC style interface
- Supports initialization, protocol negotiation, sessions, tool listing, and tool invocation
- Suitable as a stable tool interface layer for agents and bots

## 6. Key Data Model Relationships

Main pipeline:

```text
VideoSource 1 --- n VideoFile
VideoSource 1 --- n VideoSession
VideoSession 1 --- n VideoSessionFileRel --- n VideoFile
VideoSession 1 --- n EventRecord
DailySummary 1 --- 1 summary_date
```

Model descriptions:

- `VideoSource`
  - Describes a recording source
  - Contains name, location, type, configuration, enabled status, validation result
- `VideoFile`
  - A single recording file
  - Deduplicated by path hash
- `VideoSession`
  - A logical session formed from consecutive recording clips
  - Holds analysis status, summary, activity level, subjects, and other fields
- `EventRecord`
  - A structured event recording time, action, subject, importance level, offset, etc.
- `DailySummary`
  - Structured daily summary result
- `TaskLog`
  - Records task status, target, message, retries, and queue task ID

## 7. Deployment Architecture

### 7.1 Docker Compose Services

`docker-compose.yml` defines the following services:

- `postgres`
  - PostgreSQL 17
- `redis`
  - Redis, serving as Celery broker and result backend
- `backend`
  - FastAPI + Uvicorn
- `celery_worker`
  - Executes scan, analysis, daily report, webhook, and other async tasks
- `celery_beat`
  - Scheduled dispatch of heartbeat and daily report tasks
- `frontend`
  - Nginx hosting frontend static assets and proxying backend APIs

### 7.2 Container Startup Notes

- Backend image is based on `python:3.10-slim`
- `ffmpeg` is installed in the image
- Frontend image uses a two-stage build: Node for building, Nginx for serving
- `backend` depends on `postgres` and `redis` health checks
- `celery_worker` / `celery_beat` depend on `backend` health check

### 7.3 Volume Mounts

- `./xiaomi_video:/data/videos`
- `./data:/data`
- `./postgres_data:/var/lib/postgresql/data`
- `./redis_data:/data`

Where:

- `/data/videos`: Raw surveillance recordings
- `/data/hls`: Session HLS playback cache

### 7.4 Environment Variables

Key environment variables:

- `DATABASE_URL`
- `REDIS_URL`
- `SECRET_KEY`
- `MCP_TOKEN`
- `VIDEO_ROOT_PATH`
- `PLAYBACK_CACHE_ROOT`
- `DEFAULT_ADMIN_USERNAME`
- `DEFAULT_ADMIN_PASSWORD`
- `DB_INIT_MAX_RETRIES`
- `DB_INIT_RETRY_INTERVAL_SECONDS`

Note: The root `.env.example` has been updated to reflect the actual PostgreSQL, Redis, MCP, and playback cache directory configuration. Refer to `src/core/config.py` and `docker-compose.yml` as the authoritative runtime references.

Development and deployment toolchain conventions:

- Python version is standardized at 3.10
- Alembic migrations have been verified to upgrade from baseline to `head` on an empty PostgreSQL database

## 8. Frontend Pages to Backend Capability Mapping

- Dashboard → `dashboard/overview`
- Video source management → `video-sources`
- Session management → `sessions`
- Event management → `events`
- Daily report management → `daily-summaries`
- Task center → `tasks/logs`
- LLM Provider → `providers`
- Webhook → `webhooks`
- Family profile → `home-profile`
- System configuration → `system-config`
- Q&A → `chat/ask`
- Onboarding wizard → `onboarding/status`

## 9. Runtime Key Mechanisms

### 9.1 Idempotency and Deduplication

- Video files are deduplicated by `file_path_hash`
- Tasks are protected from duplicate dispatch via `TaskLog + dedupe_key + singleton guard`
- Daily reports use `summary_date` unique constraint for upsert

### 9.2 State Machine

Session analysis states include:

- `open`
- `sealed`
- `analyzing`
- `success`
- `failed`

Scan builds and task logs also have their own independent state sets.

### 9.3 Failure Recovery

- DB initialization supports retry
- Celery tasks support timeout recovery
- Analysis deadlocks support retry
- Orphaned pending tasks can be reclaimed by the heartbeat task

## 10. Testing and Quality Assurance

Backend tests are located in `tests/unit/` and `tests/integration/`.

Common commands:

```bash
pytest
pytest -v
ruff check .
ruff format .
mypy .
```

Existing test coverage includes:

- QA prompt and retrieval logic
- MCP tools and MCP HTTP interface
- Onboarding HTTP
- Daily summary tasks and output parsing
- Video source validation, webhook subscription, task dispatch binding, etc.

## 11. Current Architecture Characteristics and Recommendations

### Current Characteristics

- The architecture is pragmatic, with a clear main trunk that facilitates rapid iteration
- `api + services + tasks + models` is the actual core organizational pattern
- The `application` layer has developed some abstraction around QA / MCP / pipeline
- Frontend, backend, task system, media playback, and agent interfaces form a complete closed loop

### Recommendations

- `.env.example` should be kept in sync with the current PostgreSQL setup
- `application/bootstrap.py` can gradually evolve into a unified dependency injection entry point
- Media streaming endpoints may benefit from finer-grained authentication in the future (as noted in code comments)
- If more vendor directory formats need to be supported in the future, `adapters/` can be extended accordingly

## 12. One-Line Summary

At its core, this project is a home video intelligent analysis platform built around "directory scanning + asynchronous AI analysis + structured result service delivery".
