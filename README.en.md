> **中文版**: [README.md](README.md)

# Video Diary — AI-Powered Home Surveillance Video Analysis

> Got security cameras at home and a NAS full of recordings nobody ever watches?
>
> Video Diary watches every clip for you and tells you what happened at home today.

![Dashboard](image.png)

![Events & Playback](image-1.png)

---

## What It Does

- **Automatic video scanning** — Point it at your NAS video directory. The system discovers new recordings, deduplicates, and merges them into complete events by timeline.
- **AI-powered event recognition** — Leverages local or cloud multimodal LLMs to identify scenes like "cat jumped on the table", "delivery person arrived", or "kid came home from school".
- **Daily family digest** — Automatically generates a daily report summarizing all events, highlighting key subjects and items of concern.
- **Natural language Q&A** — Ask questions like "Did anyone linger at the front door for more than 5 minutes last night?" and get answers backed by event data.
- **Webhook / MCP integration** — Connect to WeChat bots, Home Assistant, Claude Desktop, and other external systems.
- **Web admin console** — Manage video sources, event playback, daily reports, family profiles, and model configuration — all through a visual interface.

## Quick Start

**Prerequisites**: Docker + Docker Compose

```bash
# 1. Clone the project
git clone <repo-url> && cd video_dairy

# 2. Update the video directory mount (top of docker-compose.yml)
#    Replace ./xiaomi_video with your camera recording directory

# 3. Start
docker compose up --build -d

# 4. Open your browser
#    http://localhost:8226
```

The default admin account is configured via environment variables (see "Configuration Reference" below). After startup, follow the onboarding wizard to set up video sources and model connections.

## Local LLM Deployment

The system is compatible with any model service that exposes an OpenAI-compatible API. We recommend using vLLM to deploy multimodal models locally for fully offline operation — your data never leaves the LAN.

### Hardware Requirements

| GPU | VRAM | Recommended Models |
|-----|------|--------------------|
| RTX 3090 / 3090 Ti | 24GB | MiniCPM-V 4.5 int4, MiniCPM-o 4.5 AWQ |
| RTX 4090 | 24GB | Same as above, or Qwen3.5-9B |
| Dual GPU / Higher VRAM | 48GB+ | Larger parameter models possible |

### Recommended Models

| Model | Purpose | Link |
|-------|---------|------|
| MiniCPM-V 4.5 int4 | Video understanding (primary) | [ModelScope](https://modelscope.cn/models/OpenBMB/MiniCPM-V-4_5-int4) |
| MiniCPM-o 4.5 AWQ | Video understanding (alternative) | [ModelScope](https://modelscope.cn/models/OpenBMB/MiniCPM-o-4_5-awq) |
| Qwen3.5-9B | Text summarization / daily report generation | [ModelScope](https://modelscope.cn/models/Qwen/Qwen3.5-9B) |

### vLLM Startup Example

```bash
# Install vLLM
pip install vllm

# Start the vision model (using MiniCPM-V 4.5 int4 as an example)
vllm serve OpenBMB/MiniCPM-V-4_5-int4 \
  --trust-remote-code \
  --port 8000 \
  --max-model-len 4096
```

After startup, go to "Settings → Model Connection" in the admin console and enter:

- API URL: `http://<your-IP>:8000/v1`
- Model name: must match the name used when starting vLLM

### Known Limitations

- **Ollama**: Does not support the `video` parameter; cannot be used for video analysis.
- **Bailian Platform**: Uploading videos longer than 1 minute requires a publicly accessible URL, which is not currently supported.

## Deployment

### Docker Compose Full-Stack Deployment (Recommended)

`docker-compose.yml` includes the following services:

| Service | Description |
|---------|-------------|
| postgres | Application database |
| redis | Celery message broker |
| backend | FastAPI backend |
| celery_worker | Async task execution |
| celery_beat | Scheduled task dispatch |
| frontend | React frontend + Nginx reverse proxy |

```bash
docker compose up --build -d    # Start
docker compose ps               # Check status
docker compose logs -f backend   # View backend logs
docker compose down              # Stop
```

### Offline Delivery Packaging

Suitable for NAS / intranet environments. Build images and export a delivery package:

```bash
bash scripts/package_release.sh --tag v1.0.0
```

Output is located at `output/<tag>/`, containing image archives and a streamlined `docker-compose.yml`. Users only need to change one video directory mount to get running.

### Configuration Reference

| Variable | Description | Must Change? |
|----------|-------------|--------------|
| `VIDEO_ROOT_PATH` | Video recording directory (container path) | Mounted via docker-compose |
| `SECRET_KEY` | JWT signing key | Required for production |
| `DEFAULT_ADMIN_USERNAME` | Default admin username | Recommended |
| `DEFAULT_ADMIN_PASSWORD` | Default admin password | Recommended |
| `DATABASE_URL` | PostgreSQL connection string | Keep default for Docker |
| `REDIS_URL` | Redis connection string | Keep default for Docker |
| `PLAYBACK_CACHE_ROOT` | HLS playback cache directory | Keep default for Docker |
| `MCP_TOKEN` | MCP API authentication token | Configure when using MCP |

### Default Access URLs

- Frontend: `http://localhost:8226`
- Health check: `http://localhost:8226/health`
- MCP endpoint: `http://localhost:8226/mcp`

---

<details>
<summary><strong>Developer Reference</strong></summary>

### Tech Stack

**Backend**: FastAPI / Celery / SQLAlchemy + Alembic / PostgreSQL / Redis / httpx / ffmpeg

**Frontend**: React 19 + TypeScript + Vite / React Router / @tanstack/react-query / Zustand / hls.js / Nginx

### Core Pipeline

```text
Video Directory → VideoSource → VideoFile → VideoSession → EventRecord → DailySummary
                                                         → Chat / MCP / Webhook
```

1. `heartbeat` dispatches a hot scan for each active video source every 60 seconds
2. Scans directories, parses timestamps, deduplicates by file hash, and writes to `video_file`
3. Consecutive clips are merged into `video_session` based on time gap
4. Sealed sessions are dispatched for AI analysis, producing `event_record`
5. `daily_summary` is generated on schedule or manually
6. Frontend, Q&A, MCP, and Webhook capabilities are built on top of the structured results

### Directory Structure

```text
.
├── src/
│   ├── api/                  # FastAPI routes and dependencies
│   ├── application/          # Orchestration, QA, MCP, prompt assembly
│   ├── core/                 # Configuration, security, Celery
│   ├── db/                   # Session / Alembic initialization
│   ├── infrastructure/       # Task dispatch, LLM gateway adapters
│   ├── mcp/                  # MCP server and tool implementations
│   ├── models/               # SQLAlchemy models
│   ├── providers/            # OpenAI-compatible client
│   ├── services/             # Core business services
│   └── tasks/                # Celery tasks
├── frontend/                 # Frontend project
├── alembic/                  # Database migrations
├── tests/                    # Unit / integration tests
├── docker-compose.yml
└── Dockerfile
```

For a more complete architecture overview, see [ARCHITECTURE.en.md](ARCHITECTURE.en.md).

### Local Development

```bash
# Backend (requires PostgreSQL + Redis)
pip install -r requirements.txt
python -m src.main

# Frontend
cd frontend
npm ci
npm run dev
```

Python version: 3.10

### Key API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/v1/auth/login` | Login |
| GET | `/api/v1/dashboard/overview` | Dashboard statistics |
| GET | `/api/v1/video-sources` | Video source list |
| POST | `/api/v1/tasks/{id}/build/full` | Full build |
| POST | `/api/v1/tasks/analyze/{session_id}` | Manual analysis |
| POST | `/api/v1/tasks/summarize` | Manual daily report generation |
| POST | `/api/v1/chat/ask` | Natural language Q&A |
| GET | `/api/v1/daily-summaries` | Daily report list |
| GET | `/api/v1/events` | Event list |
| POST | `/mcp` | MCP JSON-RPC endpoint |

### Common Commands

```bash
# Reset scan/session/event/task data
docker compose exec backend python -m src.reset_pipeline_data

# Tests
pytest -v

# Code checks
ruff check .
ruff format .
mypy .
```

</details>

---

<details>
<summary><strong>More Screenshots</strong></summary>

![Pet Profile](image-2.png)

![Q&A Page](image-3.png)

![Family Profile](image-4.png)

</details>
