# 家庭监控视频分析系统

这是一个面向家庭场景的离线监控视频分析系统。项目以 NAS/目录中的摄像头录像为输入，自动完成视频扫描、会话合并、AI 事件识别、家庭日报生成，并提供 Web 管理后台、自然语言问答、Webhook 与 MCP 对外能力。
对接LLM可自由选择，推荐本地搭建多模态大模型，开发者本地环境是3090ti，vllm部署。推荐模型：

1. [MiniCPM-v4.5 int4量化](https://modelscope.cn/models/OpenBMB/MiniCPM-V-4_5-int4)
2. [MiniCPM-o 4.5 int4量化](https://modelscope.cn/models/OpenBMB/MiniCPM-o-4_5-awq)

## 项目能做什么

- 自动扫描视频目录，识别新增录像并去重入库
- 将连续视频片段合并为可理解的 `Session`
- 调用兼容 OpenAI 接口的视觉模型识别关键事件并落库
- 生成按天汇总的家庭日报，提炼重点对象与关注事项
- 提供自然语言问答能力，支持围绕事件、会话、日报检索回答
- 提供 Webhook 订阅与 MCP 工具接口，方便接入 Bot / Agent / 外部系统
- 提供前端管理后台，覆盖视频源、事件、会话、任务、模型配置、家庭画像等功能

## 核心链路

```text
视频目录 -> VideoSource -> VideoFile -> VideoSession -> EventRecord -> DailySummary
                                                    -> Chat / MCP / Webhook
```

系统的主流程如下：

1. `heartbeat` 定时任务每 60 秒为启用中的视频源派发热扫描
2. 扫描任务读取目录，解析录像时间，按文件路径哈希去重写入 `video_file`
3. 连续片段按时间间隔合并为 `video_session`
4. 已封口的 Session 被派发到 AI 分析队列，生成 `event_record`
5. 定时或手动生成 `daily_summary`
6. 前端、问答、MCP、Webhook 基于这些结构化结果提供能力

## 架构总览

### 后端

- `FastAPI`：管理端 API、媒体接口、MCP 入口
- `Celery`：扫描、分析、日报、Webhook、运维任务
- `SQLAlchemy + Alembic`：数据模型与数据库迁移
- `PostgreSQL`：核心业务数据
- `Redis`：Celery Broker / Result Backend
- `httpx`：调用 OpenAI 兼容模型接口与 Webhook 推送
- `ffmpeg`：Session 合并与 HLS 播放缓存生成

### 前端

- `React 19 + TypeScript + Vite`
- `React Router`
- `@tanstack/react-query`
- `Zustand`
- `hls.js`
- `Nginx` 托管并反向代理 `/api`、`/mcp`、`/health`

### 目录结构

```text
.
├── src/
│   ├── api/                  # FastAPI 路由与依赖
│   ├── application/          # 应用编排、QA、MCP、Prompt 组装
│   ├── core/                 # 配置、安全、Celery
│   ├── db/                   # Session / Alembic 初始化
│   ├── infrastructure/       # 任务派发、LLM 网关适配
│   ├── mcp/                  # MCP 服务与工具实现
│   ├── models/               # SQLAlchemy 模型
│   ├── providers/            # OpenAI 兼容客户端
│   ├── services/             # 核心业务服务
│   └── tasks/                # Celery 任务
├── frontend/                 # 前端工程
├── alembic/                  # 数据库迁移
├── tests/                    # 单元测试 / 集成测试
├── docker-compose.yml        # 推荐部署方式
└── Dockerfile                # 后端镜像
```

更完整的架构说明见根目录 `ARCHITECTURE.md`。

## 主要功能模块

### 1. 视频源与扫描

- 支持视频源配置、路径校验、启停、暂停处理
- 当前代码内置了小米摄像头 NAS 目录解析器：`src/adapters/xiaomi_parser.py`
- 通过 `src/services/session_builder.py` 将目录扫描、文件入库、Session 合并整合为一条链路

### 2. Session 分析

- `src/tasks/analyzer.py` 将封口后的 Session 按片段切分后送入视觉模型
- 输出结构化事件，写入 `event_record`
- 同时回写 Session 级摘要、活跃度、主体、重要标记等字段

### 3. 家庭日报

- `src/tasks/summarizer.py` 支持手动生成与定时生成
- 以事件为基础，结合家庭画像生成整体总结、对象分栏和关注事项
- 当输入过长时自动切换为串行摘要模式

### 4. 自然语言问答

- API：`POST /api/v1/chat/ask`
- 基于 `src/application/qa/` 实现意图识别、检索规划、证据压缩、答案生成
- 支持引用日报、事件、Session 作为回答依据

### 5. Webhook 与 MCP

- Webhook：支持事件订阅、异步发送、测试推送
- MCP：提供 `get_daily_summary`、`search_events`、`get_event_detail`、`get_video_segments`、`ask_home_monitor` 等工具

### 6. 前端管理后台

- 已实现登录、仪表盘、视频源、会话、事件、日报、任务、模型配置、Webhook、家庭画像、系统配置、问答、引导流程等页面

## 部署方式

### 推荐方式：Docker Compose 全栈部署

项目默认通过 `docker-compose.yml` 启动以下服务：

- `postgres`
- `redis`
- `backend`
- `celery_worker`
- `celery_beat`
- `frontend`

启动：

```bash
docker compose up --build -d
```

查看状态：

```bash
docker compose ps
```

查看日志：

```bash
docker compose logs -f backend
docker compose logs -f celery_worker
docker compose logs -f celery_beat
docker compose logs -f frontend
```

停止服务：

```bash
docker compose down
```

### 本地开发运行

后端：

```bash
pip install -r requirements.txt
python -m src.main
```

前端：

```bash
cd frontend
npm ci
npm run dev
```

如果本地运行后端，需要自行准备 PostgreSQL、Redis，并通过环境变量覆盖：

- `DATABASE_URL`
- `REDIS_URL`
- `SECRET_KEY`
- `VIDEO_ROOT_PATH`
- `PLAYBACK_CACHE_ROOT`
- `DEFAULT_ADMIN_USERNAME`
- `DEFAULT_ADMIN_PASSWORD`

### 离线交付打包

项目提供 `scripts/package_release.sh`，可构建镜像并导出交付包：

```bash
bash scripts/package_release.sh --tag v1.0.0
```

输出目录位于 `output/<tag>/`，包含镜像包、`docker-compose.yml` 与交付 README，适合 NAS/内网环境部署。

打包后的交付 `docker-compose.yml` 已收敛为“单点修改视频目录”的形式，用户只需修改顶部一处视频挂载配置即可运行。

## 默认访问地址

- 前端：`http://localhost:8226`
- 前端反代健康检查：`http://localhost:8226/health`
- MCP：`http://localhost:8226/mcp`

## 初始化与运行说明

- 应用启动时会执行 Alembic 迁移，并自动确保默认管理员存在
- 当前迁移策略以 `20260320_0001` 作为正式数据库基线快照，后续结构演进通过增量 revision 管理
- 默认管理员用户名密码来自环境变量：`DEFAULT_ADMIN_USERNAME` / `DEFAULT_ADMIN_PASSWORD`
- 后端会等待 PostgreSQL 可用后再初始化，可通过下列变量调整重试窗口：

```bash
DB_INIT_MAX_RETRIES=180
DB_INIT_RETRY_INTERVAL_SECONDS=2
```

## 数据与挂载目录

`docker-compose.yml` 默认使用以下挂载：

- `./xiaomi_video:/data/videos`：原始监控视频目录
- `./data:/data`：播放缓存、HLS 文件等运行数据
- `./postgres_data:/var/lib/postgresql/data`
- `./redis_data:/data`

关键环境变量：

```bash
DATABASE_URL=postgresql+psycopg://postgres:123456@postgres:5432/home_monitor
REDIS_URL=redis://redis:6379/0
VIDEO_ROOT_PATH=/data/videos
PLAYBACK_CACHE_ROOT=/data/hls
SECRET_KEY=supersecretkey_please_change_in_production
MCP_TOKEN=change_me_mcp_token
```

其中 `MCP_TOKEN` 属于可选覆盖项；如果未显式配置，系统会使用 `src/core/config.py` 中的默认值。

## 常用运维命令

重置扫描、Session、事件与任务数据：

```bash
docker compose exec backend python -m src.reset_pipeline_data
```

运行测试：

```bash
pytest
pytest -v
```

代码检查：

```bash
ruff check .
ruff format .
mypy .
```

## 开发环境

- 后端运行、lint、mypy 与迁移基线统一使用 Python 3.10

## 关键接口概览

- `POST /api/v1/auth/login`：登录
- `GET /api/v1/dashboard/overview`：仪表盘统计
- `GET /api/v1/video-sources`：视频源列表
- `POST /api/v1/tasks/{id}/build/full`：对指定视频源执行全量构建
- `POST /api/v1/tasks/analyze/{session_id}`：手动触发 Session 分析
- `POST /api/v1/tasks/summarize`：手动生成日报
- `POST /api/v1/chat/ask`：自然语言问答
- `GET /api/v1/daily-summaries`：日报列表
- `GET /api/v1/events`：事件列表
- `POST /mcp`：MCP JSON-RPC 入口

## 界面示意

![首页](image.png)

![事件页面](image-1.png)

![宠物档案](image-2.png)

![问答页面](image-3.png)

![家庭画像](image-4.png)
