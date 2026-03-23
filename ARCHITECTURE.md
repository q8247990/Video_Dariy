# 架构文档

## 1. 文档目标

本文档基于当前代码实现，梳理项目的系统架构、模块职责、核心业务流程、关键数据模型与部署方式，便于后续维护、交付和二次开发。

## 2. 系统定位

这是一个家庭监控视频离线分析系统。

输入是 NAS 或本地目录中的监控录像文件，输出是结构化事件、可播放会话、家庭日报，以及面向前端、Bot、Agent 的查询能力。

项目不是实时流处理系统，而是以“目录扫描 + 异步分析”为核心的离线流水线系统。

## 3. 总体架构

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

视频目录 / NAS -> 扫描任务 -> VideoFile -> VideoSession -> EventRecord -> DailySummary
```

## 4. 分层与模块职责

### 4.1 `src/main.py`

- 创建 FastAPI 应用
- 注册 API Router 与 MCP Router
- 启动时执行 `init_db()`，自动跑 Alembic 迁移并初始化默认管理员
- 暴露 `/health` 与 `/health/bootstrap`

### 4.2 `src/api/`

职责：对外 HTTP 接口层。

核心文件：

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

接口特点：

- 统一通过 JWT 鉴权，依赖定义在 `src/api/deps.py`
- 提供分页列表、详情、创建、更新、触发任务等接口
- `media.py` 提供原始文件流、Session 合并流和 HLS 播放清单

### 4.3 `src/application/`

职责：应用编排层。

子模块：

- `application/pipeline/`
  - 定义命令对象和编排器
  - `PipelineOrchestrator` 用于派发构建、分析、日报、Webhook 等任务
- `application/qa/`
  - 问答用例、检索策略、证据压缩、输出 DTO
- `application/mcp/`
  - MCP 调用的服务封装
- `application/prompt/`
  - Prompt 编译与上下文合同对象

特点：

- 这里承载了“业务编排逻辑”，但不是完整 DDD；部分核心业务仍落在 `services` 中
- `src/application/bootstrap.py` 当前仅保留兼容占位，没有形成真正 DI 入口

### 4.4 `src/services/`

职责：核心领域服务。

关键服务：

- `session_builder.py`：目录扫描、去重入库、Session 合并、封口
- `session_analysis_video.py`：Session 切片、视频片段处理
- `daily_summary/*`：日报预处理与输出解析
- `home_profile.py`：家庭画像上下文构建
- `dashboard.py`：仪表盘统计
- `provider_selector.py`：视觉模型 / QA 模型选择
- `video_source_validator.py`：视频目录校验
- `webhook_payload.py` / `webhook_subscription.py`：Webhook 事件载荷与订阅判断
- `task_dispatch_control.py`：任务去重、运行态保护、任务日志绑定

这是当前项目中最重要的一层，承载了大量真实业务规则。

### 4.5 `src/tasks/`

职责：Celery 异步执行层。

关键任务：

- `session_build.py`
  - 热扫描、全量扫描
- `analyzer.py`
  - Session 视觉分析
- `summarizer.py`
  - 家庭日报生成与定时派发
- `webhook.py`
  - Webhook 异步发送
- `task_maintenance.py`
  - 心跳、超时恢复、日志清理

调度策略：

- `heartbeat`：每 60 秒运行一次
- `dispatch_scheduled_daily_summary_task`：每 60 秒运行一次
- 分析任务按优先级进入 `analysis_hot` / `analysis_full` 队列

### 4.6 `src/models/`

职责：数据库模型层。

核心实体：

- `AdminUser`：管理员账号
- `VideoSource`：视频源配置
- `VideoFile`：扫描得到的录像文件
- `VideoSession`：多个连续录像文件合并成的逻辑会话
- `VideoSessionFileRel`：Session 与文件片段顺序关系
- `EventRecord`：AI 识别出的结构化事件
- `DailySummary`：日报
- `LLMProvider`：模型服务配置
- `TaskLog`：异步任务日志与状态
- `WebhookConfig`：Webhook 配置
- `HomeProfile` / `HomeEntityProfile`：家庭画像
- `SystemConfig`：系统级配置
- `AppRuntimeState`：运行时保护状态，如日报派发 guard
- `ChatQueryLog`：问答记录
- `McpCallLog`：MCP 调用日志

### 4.7 `src/db/`

职责：数据库基础设施。

- `session.py`：Engine / SessionLocal / get_db
- `init_db.py`：Alembic 升级、默认管理员初始化、数据库可用性重试
- `base.py`：模型注册

迁移约定：

- `20260320_0001` 是当前项目的 PostgreSQL 基线快照
- 后续结构调整通过增量 Alembic revision 演进，不再依赖运行时 `create_all()` 充当迁移逻辑

### 4.8 `src/providers/` 与 `src/infrastructure/`

职责：外部系统适配。

- `providers/openai_client.py`：OpenAI 兼容接口客户端
- `infrastructure/llm/openai_gateway.py`：LLM Gateway 工厂与适配
- `infrastructure/tasks/celery_dispatcher.py`：任务派发器实现

### 4.9 `src/mcp/`

职责：MCP Server 与工具能力。

关键能力：

- 协议版本协商
- Token 鉴权
- Session 管理
- 工具列表与调用
- MCP 调用日志记录

已实现工具包括：

- `get_daily_summary`
- `search_events`
- `get_event_detail`
- `get_video_segments`
- `ask_home_monitor`

### 4.10 `frontend/`

职责：前端管理后台。

技术栈：React 19、TypeScript、Vite、React Query、Zustand、hls.js。

主要页面：

- 登录
- 仪表盘
- 视频源管理
- Session 列表
- 事件列表与详情
- 日报列表
- 任务中心
- LLM Provider 配置
- Webhook 配置
- 家庭画像
- 系统配置
- Chat 问答
- Onboarding 引导流程

Nginx 会将 `/api/`、`/mcp`、`/health` 转发到后端。

## 5. 核心业务流程

### 5.1 视频源扫描与 Session 构建

核心代码：

- `src/tasks/session_build.py`
- `src/services/session_builder.py`
- `src/adapters/xiaomi_parser.py`

流程：

1. `task_maintenance.heartbeat` 每 60 秒遍历所有启用且未暂停的视频源
2. 若当前视频源没有同类活跃扫描任务，则派发热扫描任务
3. `SessionBuilder.build()` 调用 `XiaomiDirectoryParser.scan_directory()` 扫描目录
4. 使用 `VideoFile.file_path_hash` 去重，避免重复入库和重复分析
5. 新文件按时间顺序追加到当前 open Session，若相邻片段间隔大于 61 秒则创建新 Session
6. 热扫描模式下保留最近 open Session，历史 open Session 被 seal
7. 若 open Session 在 600 秒内没有新片段，也会被 seal
8. seal 后自动派发分析任务

关键规则：

- 合并阈值：`MERGE_GAP_SECONDS = 61`
- 封口缓冲：`SEAL_BUFFER_SECONDS = 600`
- 扫描模式：`hot` / `full`

### 5.2 Session 分析与事件生成

核心代码：

- `src/tasks/analyzer.py`
- `src/services/session_analysis_video.py`
- `src/services/video_analysis/output_parser.py`
- `src/services/video_analysis/mapper.py`

流程：

1. 任务只允许从 `SEALED` 状态抢占为 `ANALYZING`
2. 将 Session 按 `ANALYZER_SEGMENT_SECONDS` 切片，默认 600 秒
3. 为每个切片构造视频数据 URL 与识别 Prompt
4. 调用兼容 OpenAI 的视觉模型
5. 解析返回 JSON，转换为多个 `EventRecord`
6. 覆盖替换该 Session 历史事件
7. 汇总片段摘要并回写到 `VideoSession`
8. 更新分析状态为 `SUCCESS`

附加机制：

- 任务日志绑定与状态落库
- token quota 检查与 token usage 记录
- 死锁重试与分析状态回滚
- 失败时保留原始模型返回摘要片段，方便排查

### 5.3 家庭日报生成

核心代码：

- `src/tasks/summarizer.py`
- `src/services/daily_summary/preprocess.py`
- `src/services/daily_summary/output_parser.py`
- `src/application/prompt/compiler.py`

流程：

1. Beat 每 60 秒检查是否到达日报生成时间
2. 默认针对“昨天”的数据生成日报
3. 从 `EventRecord` 查询当天事件
4. 结合家庭画像提取已知对象、主题映射和关注事项候选
5. 如果 prompt 规模较小，走 `single_pass`
6. 如果 prompt 规模过大，走 `split_serial`：先按对象生成摘要，再生成总述
7. 将结果裁剪到更适合展示的长度区间
8. 以 `summary_date` 为唯一键执行 upsert
9. 若配置了相关 Webhook 订阅，派发 `daily_summary_generated`

保护机制：

- `AppRuntimeState` 中的 dispatch guard，避免同一分钟重复派发
- 已有 summary 或运行中任务时不会重复生成

### 5.4 自然语言问答

核心代码：

- `src/api/v1/endpoints/chat.py`
- `src/application/qa/service.py`
- `src/application/qa/planner.py`
- `src/application/qa/retriever.py`
- `src/application/qa/evidence_compressor.py`

流程：

1. 接收问题文本
2. 选择 QA Provider
3. 构建家庭上下文
4. 用 LLM 输出 QueryPlan / RetrievalPlan
5. 分层检索日报、Session、事件
6. 压缩证据，减少上下文开销
7. 再次调用模型生成最终回答
8. 记录 `ChatQueryLog`

特点：

- 不是简单的全文搜索，而是“理解问题 -> 规划检索 -> 组织证据 -> 回答”
- 可返回引用的事件与 Session

### 5.5 Webhook 推送

核心代码：

- `src/api/v1/endpoints/webhooks.py`
- `src/tasks/webhook.py`
- `src/services/webhook_payload.py`
- `src/services/webhook_subscription.py`

流程：

1. 用户在后台配置 Webhook 地址、启用状态、订阅事件
2. 系统在事件触发点构造标准载荷
3. Celery 任务异步发送 HTTP POST
4. 订阅匹配逻辑支持标准化的 `event_subscriptions_json`

### 5.6 MCP 能力

核心代码：

- `src/mcp/server.py`
- `src/mcp/tools.py`
- `src/application/mcp/service.py`

特点：

- 使用 JSON-RPC 风格接口
- 支持初始化、协议协商、会话、工具列表、工具调用
- 适合作为 Agent / Bot 的稳定工具接口层

## 6. 关键数据模型关系

主链路：

```text
VideoSource 1 --- n VideoFile
VideoSource 1 --- n VideoSession
VideoSession 1 --- n VideoSessionFileRel --- n VideoFile
VideoSession 1 --- n EventRecord
DailySummary 1 --- 1 summary_date
```

模型说明：

- `VideoSource`
  - 描述一个录像来源
  - 包含名称、位置、类型、配置、启用状态、校验结果
- `VideoFile`
  - 单个录像文件
  - 通过路径哈希去重
- `VideoSession`
  - 连续录像片段形成的逻辑会话
  - 持有分析状态、摘要、活跃度、主体等字段
- `EventRecord`
  - 结构化事件，记录时间、动作、对象、重要程度、偏移量等
- `DailySummary`
  - 按天汇总的结构化结果
- `TaskLog`
  - 记录任务状态、目标、消息、重试、队列任务 ID

## 7. 部署架构

### 7.1 Docker Compose 服务

`docker-compose.yml` 定义了以下服务：

- `postgres`
  - PostgreSQL 17
- `redis`
  - Redis，作为 Celery broker 与 backend
- `backend`
  - FastAPI + Uvicorn
- `celery_worker`
  - 执行扫描、分析、日报、Webhook 等异步任务
- `celery_beat`
  - 定时派发心跳与日报任务
- `frontend`
  - Nginx 托管前端静态资源并代理后端接口

### 7.2 容器启动要点

- 后端镜像基于 `python:3.10-slim`
- 镜像中安装 `ffmpeg`
- 前端镜像为两阶段构建：Node 构建，Nginx 运行
- `backend` 依赖 `postgres` 和 `redis` 健康检查
- `celery_worker` / `celery_beat` 依赖 `backend` 健康检查

### 7.3 挂载约定

- `./xiaomi_video:/data/videos`
- `./data:/data`
- `./postgres_data:/var/lib/postgresql/data`
- `./redis_data:/data`

其中：

- `/data/videos`：原始监控录像
- `/data/hls`：Session HLS 播放缓存

### 7.4 环境变量

关键环境变量如下：

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

说明：当前根目录 `.env.example` 已按 PostgreSQL、Redis、MCP 与播放缓存目录的实际配置同步更新，推荐以 `src/core/config.py` 和 `docker-compose.yml` 为最终运行准则。

开发与部署工具链约定：

- Python 版本统一为 3.10
- Alembic 迁移已验证可在空 PostgreSQL 数据库上从基线升级到 `head`

## 8. 前端页面与后端能力映射

- 仪表盘 -> `dashboard/overview`
- 视频源管理 -> `video-sources`
- 会话管理 -> `sessions`
- 事件管理 -> `events`
- 日报管理 -> `daily-summaries`
- 任务中心 -> `tasks/logs`
- LLM Provider -> `providers`
- Webhook -> `webhooks`
- 家庭画像 -> `home-profile`
- 系统配置 -> `system-config`
- 问答 -> `chat/ask`
- 引导流程 -> `onboarding/status`

## 9. 运行时关键机制

### 9.1 幂等与去重

- 视频文件通过 `file_path_hash` 去重
- 任务通过 `TaskLog + dedupe_key + singleton guard` 防止重复派发
- 日报通过 `summary_date` 唯一约束 upsert

### 9.2 状态机

Session 分析状态主要包括：

- `open`
- `sealed`
- `analyzing`
- `success`
- `failed`

扫描构建、任务日志也有独立的运行状态集合。

### 9.3 故障恢复

- DB 初始化支持重试
- Celery 任务支持超时恢复
- 分析死锁支持重试
- 孤儿 pending 任务可被心跳任务回收

## 10. 测试与质量保障

后端测试位于 `tests/unit/` 与 `tests/integration/`。

常用命令：

```bash
pytest
pytest -v
ruff check .
ruff format .
mypy .
```

现有测试覆盖了：

- QA Prompt 与检索逻辑
- MCP 工具与 MCP HTTP 接口
- Onboarding HTTP
- Daily Summary 任务与输出解析
- 视频源校验、Webhook 订阅、任务派发绑定等

## 11. 当前架构特点与建议

### 当前特点

- 架构偏实用主义，主干清晰，便于快速迭代
- `api + services + tasks + models` 是当前真实的核心组织方式
- `application` 层已经在 QA / MCP / pipeline 上形成一定抽象
- 前后端、任务系统、媒体回放和 Agent 接口已经形成完整闭环

### 建议关注点

- `.env.example` 需要与 PostgreSQL 现状保持一致
- `application/bootstrap.py` 可逐步演进为统一依赖注入入口
- 媒体流接口目前注释中也提到，后续可补更细粒度鉴权
- 如果后续支持更多厂商目录格式，可继续扩展 `adapters/`

## 12. 一句话总结

这个项目的本质，是一个以“目录扫描 + 异步 AI 分析 + 结构化结果服务化”为核心的家庭视频智能分析平台。
