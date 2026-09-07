> **English version**: [ARCHITECTURE.en.md](ARCHITECTURE.en.md)

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
- 暴露 `/health`、`/livez`、`/readyz`、`/metrics` 与 `/health/bootstrap`

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
- `src/api/v1/endpoints/dashboard.py`
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

- `session_build/`：按 discovery、dedupe、reducer、persistence、seal_policy 与 runner 拆分目录扫描、去重入库、Session 合并和封口
- `analysis/`：按 claim、chunk plan、sub-chunk、checkpoint、聚合与 finalize 拆分 Session 分析
- `summarizer/`：按调度、证据、生成、解析与 finalize 拆分日报生成
- `dispatch/` 与 `maintenance/`：任务 claim/lease/dedupe/cancel，以及热调度、超时恢复、文件/日志清理
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
- `LLMProvider`：模型服务配置与能力标记
- `TaskLog`：异步任务日志与状态
- `WebhookConfig`：Webhook 配置
- `HomeProfile` / `HomeEntityProfile`：家庭画像
- `SystemConfig`：系统级配置
- `AppRuntimeState`：运行时保护状态，如日报派发 guard
- `ChatQueryLog`：问答记录
- `McpCallLog`：MCP 调用日志
- `LLMUsageLog`：LLM Token 用量记录
- `TagDefinition`：标签定义
- `EventTagRel`：事件与标签多对多关联
- `VideoSourceRuntimeState`：视频源运行时状态（延迟告警等）

### 4.7 `src/db/`

职责：数据库基础设施。

- `session.py`：Engine / SessionLocal / get_db
- `init_db.py`：Alembic 升级、默认管理员初始化、数据库可用性重试、迁移咨询锁
- `readiness.py`：`/livez` / `/readyz` 的健康检查实现
- `metrics.py`：`/metrics` 的可观测性指标（outbox 延迟/失败、任务恢复、checkpoint 进度）
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

- `get_data_availability`
- `search_events`
- `get_sessions`
- `get_daily_summary`
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
- `src/services/session_build/runner.py`
- `src/adapters/xiaomi_parser.py`

流程：

1. `task_maintenance.heartbeat` 每 60 秒遍历所有启用且未暂停的视频源
2. 若当前视频源没有同类活跃扫描任务，则派发热扫描任务
3. `session_build.runner.run()` 经 discovery 阶段调用 `XiaomiDirectoryParser.scan_directory()` 扫描目录
4. 使用 `VideoFile.file_path_hash` 去重，避免重复入库和重复分析
5. 新文件按时间顺序追加到当前 open Session，若相邻片段间隔大于 61 秒则创建新 Session
6. 热扫描模式下保留最近 open Session，历史 open Session 被 seal
7. 若 open Session 在 600 秒内没有新片段，也会被 seal
8. seal 后自动派发分析任务

关键规则：

- 合并阈值：`MERGE_GAP_SECONDS = 1`
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
2. 将 Session 按 `ANALYZER_SEGMENT_SECONDS` 切片，默认 600 秒（10 分钟 session-level chunk）
3. 对每个 session chunk 内部按 `ANALYZER_LLM_CHUNK_SECONDS`（默认 60 秒）再切分为 sub-chunk；每个 sub-chunk 调一次视觉模型
4. LLM 载荷固定为 `raw_mp4`：直接将 sub-chunk mp4 base64 传给视觉模型，并通过 `media_io_kwargs.video.num_frames` 请求服务端均匀采样。客户端不再保留关键帧解码、MAD/pHash 或 JPEG 预处理路径。
5. 为每个 sub-chunk 构造 LLM Prompt（保留 sub-chunk 偏移，`base_offset_seconds = sub_chunk.start_offset_seconds`）
6. 调用兼容 OpenAI 的视觉模型（payload 通过 `chat_completion(..., extra_body={...})` 注入 `media_io_kwargs`）
7. 解析返回 JSON，转换为多个 `EventRecord`（`offset` 相对 session 起始时间，非负）
8. 覆盖替换该 Session 历史事件
9. 汇总片段摘要并回写到 `VideoSession`
10. 更新分析状态为 `SUCCESS`

分析与断点续跑：

- 任务通过 `_claim_session_for_analysis` 抢占：一个原子的条件状态更新
  （`transition_session` 的 `UPDATE ... WHERE id AND status IN ('sealed','partial')`）加上
  commit，随后的 SELECT 仅用于取回该行。这是原子的 compare-and-set，不是
  selection-then-update，因此不存在并发抢占竞态。
- 每个 sub-chunk 的处理进度写入持久化 `SessionAnalysisCheckpoint`（按
  session + analysis_run + sub_chunk 的唯一工作键，记录输入指纹、状态、事件载荷、Token
  用量与错误）。中途失败时 Session 进入 `PARTIAL` 状态；重跑会从第一个非 success 断点
  继续，绝不重复计费成功分片。Token 用量按 session + checkpoint 归属到 `LLMUsageLog`。
- 最终化阶段将已完成断点合并为 Session 可见事件集；日报、问答与 MCP 只消费
  `analysis_status == 'success'` 的 Session。

附加机制：

- 任务日志绑定与状态落库；detail_json 记录 sub-chunk 计划和执行结果
- token quota 检查与 token usage 记录
- 死锁重试与分析状态回滚
- 失败时保留原始模型返回摘要片段，方便排查
- 关键帧提取 ffmpeg stderr 末 1KB 截断（异常信息不污染 TaskLog）

### 5.3 家庭日报生成

核心代码：

- `src/tasks/summarizer.py`
- `src/services/summarizer/`
- `src/application/prompt/compiler.py`

流程：

1. Beat 每 60 秒检查是否到达日报生成时间
2. 默认针对“昨天”的数据生成日报
3. 从 `EventRecord` 查询当天事件
4. 结合家庭画像提取已知对象、主题映射和关注事项候选
5. 如果 prompt 规模较小，走 `single_pass`
6. 如果 prompt 规模过大，走 `split_serial`：先按对象生成摘要，再生成总述
7. 将结果裁剪到更适合展示的长度区间
8. 以 `summary_date` 为唯一键发布日报，并以 `DailySummaryGenerationAttempt` 保留每次尝试
9. 对每个匹配订阅者写入一个定向 webhook outbox 事件，事件类型为 `daily_summary_generated`

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
  - 记录任务状态、目标、消息、重试、队列任务 ID（业务运行生命周期）
- `OutboxEvent`（事务性 outbox，与 `TaskLog` 1:1）
  - 记录发布意图：Celery 任务名、队列、payload、状态（pending/publishing/published/failed）、重试与租约
  - 独立 publisher 进程以其 `event_id` 作为 broker `task_id`（ADR 0011）
- `DailySummaryGenerationAttempt`
  - 按 `summary_date` append-only 记录每次日报生成尝试（含失败/超时原因）
- `PipelineTransitionLog`
  - 管道状态机（`VideoSession` / `TaskLog`）的 append-only 审计，不随 `TaskLog` 7 天清理丢失
- `SessionAnalysisCheckpoint`
  - 分析断点续跑与进度；`/metrics` 的 checkpoint 进度来源
- `AppRuntimeState`
  - 运行时保护状态，另存 heartbeat 指标快照（`heartbeat_last_counters`）
- `LLMUsageLog`
  - LLM Token 用量归账

## 7. 部署架构

### 7.1 Docker Compose 服务

`docker-compose.yml` 定义了以下服务（4 容器拓扑）：

- `postgres`
  - PostgreSQL 17
- `redis`
  - Redis，作为 Celery broker 与 backend
- `backend`
  - 通过 supervisord（见 `supervisord.conf`）托管 5 个进程：
    - `api`：FastAPI + Uvicorn
    - `worker`：消费 `celery` 队列（扫描、日报、Webhook、维护），concurrency=2
    - `vision-worker`：以 `concurrency=1` 消费 `analysis_hot` / `analysis_full` 队列
    - `beat`：定时派发心跳与日报任务（单实例）
    - `outbox-publisher`：事务性 outbox 发布器
- `frontend`
  - Nginx 托管前端静态资源并代理后端接口

### 7.2 容器启动要点

- 后端镜像基于 `python:3.10-slim`
- 镜像中安装 `ffmpeg` 与 `supervisor`
- 前端镜像为两阶段构建：Node 构建，Nginx 运行
- `backend` 依赖 `postgres` 和 `redis` 健康检查；`frontend` 依赖 `backend` 健康检查
- `backend` 容器内任一进程崩溃由 supervisord 自动拉起；进程级健康可用
  `/metrics`（outbox 延迟、任务恢复计数）与 `docker top hm_backend` 观察
- 重启 / 重建 `backend` 会中断进行中的任务：`acks_late` 使消息回队重投，
  分析断点续跑保证不丢数据、不重复计费

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

- `APP_ENV`（`production` 强制 `SECRET_KEY` / `MEDIA_SIGNING_KEY` 互不相同并校验
  `PROVIDER_KEY_ENCRYPTION_KEY` 格式）
- `DATABASE_URL`
- `REDIS_URL`
- `SECRET_KEY`
- `MEDIA_SIGNING_KEY`（媒体签名密钥，必须不同于 `SECRET_KEY`）
- `PROVIDER_KEY_ENCRYPTION_KEY`（`v1:<Fernet key>` 格式的 Provider API-key 静态加密密钥）
- `MCP_TOKEN`
- `VIDEO_ROOT_PATH`
- `PLAYBACK_CACHE_ROOT`
- `DEFAULT_ADMIN_USERNAME`
- `DEFAULT_ADMIN_PASSWORD`
- `DB_INIT_MAX_RETRIES`
- `DB_INIT_RETRY_INTERVAL_SECONDS`
- `DEFAULT_LOCALE`
- `ACCESS_TOKEN_EXPIRE_MINUTES`
- `ENTITY_IMAGE_ROOT`
- `SESSION_PLAYBACK_MODE`
- `MANIFEST_TTL_SECONDS` / `SEGMENT_TTL_SECONDS`（媒体签名 URL 有效期，默认 1800 秒）
- `WEBHOOK_PRIVATE_NETWORK_ALLOWLIST`（Webhook SSRF 防护的内网放行列表）
- `ANALYZER_SEGMENT_SECONDS`
- `ANALYZER_LLM_CHUNK_SECONDS`（默认 60；每个 LLM 调用的 sub-chunk 时长）
- （以下仅保留兼容，属 inert——视频预处理为 raw_mp4-only，无法重新启用关键帧路径）
  `ANALYZER_VIDEO_KEYFRAME_PERIOD_SECONDS`（默认 8）、
  `ANALYZER_VIDEO_KEYFRAME_MAD_THRESHOLD`（默认 1.0）、
  `ANALYZER_VIDEO_KEYFRAME_PHASH_THRESHOLD`（默认 6）、
  `ANALYZER_VIDEO_KEYFRAME_FALLBACK_TO_MP4`（默认 true）

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

Session 分析状态（`analysis_status`）主要包括：

- `open`
- `sealed`
- `analyzing`
- `partial`（部分分析：存在已成功断点但未全部完成，可续跑）
- `success`
- `failed`

`pipeline_state.py` 集中定义并执行 `VideoSession` 与 `TaskLog` 的合法状态迁移，同时写入 append-only 的 `PipelineTransitionLog` 审计。扫描构建与任务日志有各自的运行状态集合。

### 9.3 媒体生命周期与源删除（外键策略）

媒体模型遵循以下外键删除策略（迁移 `20260826_0017`）：

- `video_file.source_id` -> `video_source`：`CASCADE`
- `video_source_runtime_state.source_id` -> `video_source`：`CASCADE`
- `video_session.source_id` -> `video_source`：`RESTRICT`
- `event_record.source_id` -> `video_source`：`RESTRICT`（保留事件历史；删除视频源在存在
  残留历史时以业务码 4004 拒绝）
- `event_record.session_id` -> `video_session`：`CASCADE`
- `video_session_file_rel`（session_id / video_file_id）-> `video_session` / `video_file`：`CASCADE`
- `event_tag_rel.event_id` -> `event_record`：`CASCADE`
- `session_analysis_checkpoint.session_id` -> `video_session`：`CASCADE`

迁移 `20260826_0017` 在改写外键前通过 `_assert_no_existing_orphans()` 预检，存在悬空引用
时中止迁移。源删除由 `delete_video_source` 按此策略实施，并阻塞在保留的历史事件上。

### 9.4 派发与源扫描串行化 + 租约恢复

- 同一视频源同时只允许一类活跃扫描任务（`TaskLog + dedupe_key + singleton guard`），
  从派发层面避免并发扫描同一目录。
- 分析任务按优先级进入 `analysis_hot` / `analysis_full` 队列；`vision-worker`
  进程（backend 容器内由 supervisord 托管）以 concurrency=1 串行消费，从执行层面
  避免同一队列内的并行分析。
- 心跳任务会回收孤儿 pending 任务并做超时恢复（租约恢复）：进程崩溃后遗留的运行态可被
  心跳重新认领，配合原子抢占与分析断点实现断点续跑。

### 9.5 家庭时区

`home_timezone` 系统配置项使用 IANA 时区（默认 `Asia/Shanghai`，经 ZoneInfo 校验；写入
非法时区会被拒绝）。所有时间戳均以带时区的 UTC 存储；日报等"按天"边界通过
`home_timezone` 计算本地一天的范围。迁移 `20260825_0014` 将历史 naive 的
`Asia/Shanghai` 墙钟时间按 `AT TIME ZONE 'Asia/Shanghai'` 重新解释为 UTC 时刻。

### 9.6 错误状态中间件

`ResponseStatusMiddleware` 将携带业务码的 JSON 响应映射为 HTTP 状态（见
`src/api/error_status.py` 的 `_STATUS_BY_CODE`）：4000→400、4001→409、4002→404、
4004→409、4011→401、4290→429、4291→429、5000→500、5001→502、5002→503。前端通过
`authRecovery.ts` 幂等锁处理 HTTP 401 与业务码 4011，清理登录态并跳转登录页。

### 9.7 故障恢复

- DB 初始化支持重试
- Celery 任务支持超时恢复
- 分析死锁支持重试
- 孤儿 pending 任务可被心跳任务回收

### 9.8 可观测性与运维

- **结构化 JSON 日志与脱敏**：`src/core/logging_config.py` 提供
  `configure_logging()`（单行 JSON 根 handler）、`RedactingJsonFormatter` 与
  `redact()`（擦除密钥、连接串、敏感 JSON 值、`VIDEO_ROOT_PATH` 路径）。
- **关联 ID**：outbox `event_id` 经 FastAPI `CorrelationMiddleware`、
  调度日志、outbox 发布日志、Celery `task_prerun` 信号串联，单 id 可检索
  API → outbox → worker 全链路。
- **健康探针与指标**：`/livez`、`/readyz`（DB + Redis + alembic head）、
  `GET /metrics`（outbox 延迟/失败、任务恢复计数、checkpoint 进度）。
- **备份/恢复**：`scripts/backup_restore_db.py` 提供 checksum 化的
  backup / verify / restore，用于不可逆迁移前的已验证备份。
- **迁移串行化**：`init_db` 通过 PostgreSQL 咨询锁（`pg_advisory_lock`）
  串行化容器启动时的 Alembic 迁移。
- **至多一次 vs 至少一次**：投递为 **至少一次（at-least-once）**，消费侧
  幂等短路兜底；文中任何描述均不主张 exactly-once。

## 10. 测试与质量保障

后端测试位于 `tests/unit/` 与 `tests/integration/`。

常用命令：

```bash
python3 -m pytest tests/unit -q         # 单元测试
python3 -m pytest -m postgres           # 集成测试，需真实 PostgreSQL（DATABASE_URL）
python3 -m alembic upgrade head
python3 -m alembic heads                # 期望 20260904_0022
ruff check .
ruff format --check src tests
# 已知例外：ruff format --check 在
# src/application/prompt/compiler.py、src/application/qa/agent.py、
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
