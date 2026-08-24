# AGENTS.md - 项目协作规范

全程使用中文进行思考和回复。

## 1. 文档目的

本文档用于指导 Agent 在本仓库内进行分析、开发、调试、测试和文档维护。

目标是让 Agent：

- 快速理解项目定位、核心架构与关键业务链路
- 按照现有工程约定修改代码，避免破坏已有行为
- 在完成任务时优先遵循项目真实结构，而不是套用通用模板

## 2. 项目概览

这是一个面向家庭场景的离线监控视频分析系统。

- 输入：NAS 或本地目录中的监控录像文件
- 处理：目录扫描、去重入库、Session 合并、AI 分析、日报生成
- 输出：结构化事件、可播放会话、家庭日报，以及面向前端、Webhook、MCP、问答能力的接口

项目核心链路：

```text
视频目录 -> VideoSource -> VideoFile -> VideoSession -> EventRecord -> DailySummary
                                                    -> Chat / MCP / Webhook
```

这是一个“目录扫描 + 异步分析”的离线流水线系统，不是实时流处理系统。

## 3. 技术栈与部署形态

### 3.1 后端

- `FastAPI`：REST API、媒体接口、MCP 入口
- `Celery`：扫描、分析、日报、Webhook、维护任务
- `SQLAlchemy + Alembic`：数据模型与迁移
- `PostgreSQL`：核心业务数据存储
- `Redis`：Celery Broker / Result Backend
- `httpx`：调用 OpenAI 兼容模型与 Webhook
- `ffmpeg`：视频片段处理、Session 合并、HLS 缓存生成

### 3.2 前端

- `React 19 + TypeScript + Vite`
- `React Router`
- `@tanstack/react-query`
- `Zustand`
- `hls.js`
- `Nginx`：托管前端并反代 `/api`、`/mcp`、`/health`

### 3.3 推荐部署方式

项目默认通过 `docker-compose.yml` 部署以下服务：

- `postgres`
- `redis`
- `backend`
- `celery_worker`
- `celery_beat`
- `frontend`

## 4. 目录与模块职责

以当前代码实现为准，重点关注以下目录：

```text
.
├── src/
│   ├── api/              # FastAPI 路由、鉴权依赖、HTTP 接口
│   ├── application/      # 应用编排、QA、MCP、Prompt 组织
│   ├── core/             # 配置、安全、Celery 初始化
│   ├── db/               # Session、模型注册、数据库初始化
│   ├── infrastructure/   # 外部系统与任务派发适配
│   ├── mcp/              # MCP 服务与工具实现
│   ├── models/           # SQLAlchemy 模型
│   ├── providers/        # OpenAI 兼容客户端
│   ├── services/         # 核心业务规则
│   └── tasks/            # Celery 异步任务
├── frontend/             # 前端管理后台
├── tests/                # 单元测试与集成测试
├── alembic/              # 数据库迁移
├── docker-compose.yml
└── ARCHITECTURE.md
```

### 4.1 关键后端层次

- `src/main.py`
  - 创建 FastAPI 应用
  - 注册 API Router 与 MCP Router
  - 启动时执行数据库初始化与迁移
- `src/api/`
  - 对外 HTTP 接口层
  - 统一通过 `src/api/deps.py` 处理鉴权与依赖
- `src/application/`
  - 承载编排逻辑，不是纯领域层
  - 重点包括 pipeline、QA、MCP、Prompt 组织
- `src/services/`
  - 当前项目最重要的业务层
  - 大部分真实业务规则都在这里
- `src/tasks/`
  - Celery 任务执行入口
  - 负责调度扫描、分析、日报、Webhook、维护任务
- `src/models/`
  - 持久化模型定义
- `src/mcp/`
  - MCP Server 与工具能力

### 4.2 关键服务与任务

- `src/services/session_builder.py`
  - 扫描目录、去重入库、Session 合并、封口
- `src/tasks/session_build.py`
  - 热扫描、全量扫描任务入口
- `src/tasks/analyzer.py`
  - Session 视觉分析任务
- `src/tasks/summarizer.py`
  - 家庭日报生成任务
- `src/tasks/webhook.py`
  - Webhook 异步发送
- `src/tasks/task_maintenance.py`
  - 心跳、超时恢复、日志清理

## 5. 核心业务流程

### 5.1 视频扫描与 Session 构建

主流程：

1. `heartbeat` 每 60 秒扫描启用中的视频源
2. 若无同类活跃任务，则派发热扫描任务
3. 扫描目录并解析录像文件
4. 使用 `VideoFile.file_path_hash` 去重入库
5. 按时间连续性合并为 `VideoSession`
6. 满足封口条件后将 Session 标记为 `sealed`
7. 自动派发后续分析任务

关键规则：

- 合并阈值：`MERGE_GAP_SECONDS = 1`
- 封口缓冲：`SEAL_BUFFER_SECONDS = 600`
- 扫描模式：`hot` / `full`

### 5.2 Session 分析

主流程：

1. 仅允许 `SEALED` 状态进入分析
2. 按片段切分视频，默认片长 600 秒（`ANALYZER_SEGMENT_SECONDS`）
3. 内部按 `ANALYZER_LLM_CHUNK_SECONDS`（默认 300 秒）将每个 session chunk 再切分为 sub-chunk，每个 sub-chunk 调一次视觉模型
4. 调用兼容 OpenAI 的视觉模型（payload 见下方"视频预处理管线"）
5. 解析模型输出并生成 `EventRecord`（`base_offset_seconds = sub_chunk.start_offset_seconds`，保留绝对 session 时间）
6. 回写 Session 摘要、活跃度、主体、重要标记等信息
7. 将分析状态更新为成功或失败

**视频预处理管线**（基于 `REPORT_video_preprocessing.md` §4.1）：

- 当 `LLMProvider.video_preprocess_mode = "keyframe"`（默认）时，客户端用 ffmpeg 单遍解码源 mp4（2fps 采样 + 在线 MAD/pHash 决策 + top-N JPEG 关键帧），把 `data:video/jpeg;base64,<J1>,<J2>,...` 与 `media_io_kwargs.video = {fps, total_num_frames, frames_indices, num_frames: -1}` 一起发给视觉模型
- 关键帧提取参数受 `LLMProvider.video_keyframe_target_n` / `video_keyframe_jpeg_quality` 控制；提取阈值由系统设置 `ANALYZER_VIDEO_KEYFRAME_*` 控制
- 当 `video_preprocess_mode = "raw_mp4"` 或关键帧提取失败且 `ANALYZER_VIDEO_KEYFRAME_FALLBACK_TO_MP4=true` 时，回退到旧 `data:video/mp4;base64,...` 路径
- vLLM 端零改动

### 5.3 家庭日报

主流程：

1. 定时检查是否到达日报生成时间
2. 默认对“昨天”的事件数据生成日报
3. 结合家庭画像构建摘要上下文
4. 输入较小时走单次摘要，较长时走串行摘要
5. 以 `summary_date` 为唯一键执行 upsert
6. 如存在订阅，触发 `daily_summary_generated` Webhook

### 5.4 问答、Webhook 与 MCP

- 问答：基于“理解问题 -> 规划检索 -> 组织证据 -> 生成回答”的链路实现
- Webhook：支持订阅、异步投递、测试推送
- MCP：已提供 `get_daily_summary`、`search_events`、`get_event_detail`、`get_video_segments`、`ask_home_monitor` 等工具

## 6. 关键数据模型

开发时优先理解这些核心实体：

- `VideoSource`：视频源配置
- `VideoFile`：扫描得到的原始录像文件
- `VideoSession`：连续录像片段聚合后的逻辑会话
- `VideoSessionFileRel`：Session 与文件的顺序关系
- `EventRecord`：AI 识别后的结构化事件
- `DailySummary`：按天生成的家庭日报
- `LLMProvider`：模型服务配置（含 `video_preprocess_mode` / `video_keyframe_target_n` / `video_keyframe_jpeg_quality` 三个视频预处理字段；见 §5.2）
- `TaskLog`：异步任务日志与状态
- `WebhookConfig`：Webhook 配置
- `HomeProfile` / `HomeEntityProfile`：家庭画像
- `SystemConfig`：系统配置
- `AdminUser`：管理员账号
- `AppRuntimeState`：运行时保护状态
- `ChatQueryLog`：问答记录
- `McpCallLog`：MCP 调用日志
- `LLMUsageLog`：LLM Token 用量记录
- `TagDefinition` / `EventTagRel`：标签定义与事件关联
- `VideoSourceRuntimeState`：视频源运行时状态

主关系链路：

```text
VideoSource 1 --- n VideoFile
VideoSource 1 --- n VideoSession
VideoSession 1 --- n VideoSessionFileRel --- n VideoFile
VideoSession 1 --- n EventRecord
DailySummary 1 --- 1 summary_date
```

## 7. 开发原则

### 7.1 总体原则

- 优先遵循现有架构与命名，不要引入与项目风格冲突的新分层
- 小步修改，避免无关重构
- 修复问题时优先定位根因，不做表面补丁
- 改动前先理解业务链路、状态流转与数据模型关系
- 涉及任务调度、幂等、状态机时必须谨慎，避免重复派发或状态污染

### 7.2 分层约定

- HTTP 参数校验、响应组织放在 `api/`
- 跨模块编排放在 `application/`
- 核心业务规则优先放在 `services/`
- 异步执行入口放在 `tasks/`
- 持久化结构定义放在 `models/`
- 外部系统适配放在 `providers/` 或 `infrastructure/`

如果只是修改接口，不要把业务规则塞进路由层。

### 7.3 变更时的关注点

- 扫描逻辑：关注去重、时间解析、Session 合并与封口规则
- 分析逻辑：关注状态抢占、切片策略、结果覆盖、失败恢复
- 日报逻辑：关注日期范围、去重生成、输入裁剪和 guard
- 问答逻辑：关注检索计划、证据压缩和引用链路
- Webhook / MCP：关注鉴权、协议兼容、日志记录和异常处理

## 8. 代码风格

### 8.1 Python 约定

- 遵循 PEP 8
- 使用 4 空格缩进，不使用 Tab
- 单行最大长度 100
- 顶级定义之间保留 2 个空行
- 新增或修改函数时优先补充类型标注
- 优先使用清晰、可维护的实现，不写炫技代码

### 8.2 导入规范

- 优先使用绝对导入，例如 `from src.services.dashboard import DashboardService`
- 导入分组顺序：标准库、第三方库、本地模块
- 不使用通配符导入

### 8.3 命名规范

- 变量、函数：`snake_case`
- 类：`PascalCase`
- 常量：`UPPER_SNAKE_CASE`
- 私有方法或内部辅助函数：前缀 `_`

### 8.4 类型与数据结构

- 为函数签名补充明确类型
- 尽量避免滥用 `Any`
- 与现有代码风格保持一致；如项目中某模块已统一使用某种写法，优先延续该模块风格

### 8.5 异常与日志

- 捕获明确的异常类型，不要裸 `except`
- 错误信息要包含上下文，便于排查任务链路问题
- 使用 `logging`，不要使用 `print()`
- 记录任务、外部调用、状态切换时尽量带上对象 ID 或任务 ID

## 9. 测试与质量要求

### 9.1 常用命令

运行后端：

```bash
pip install -r requirements.txt
python -m src.main
```

运行环境基线：

- Python 统一使用 3.10
- 数据库结构以 Alembic revision 为准，不依赖运行时自动建表

代码检查：

```bash
ruff check .
ruff check --fix .
ruff format .
mypy .
```

测试：

```bash
pytest
pytest -v
pytest tests/test_example.py
pytest tests/test_example.py::test_function_name
pytest -k "test_pattern"
pytest --cov=src --cov-report=html
```

前端开发：

```bash
cd frontend
npm ci
npm run dev
```

### 9.2 测试要求

- 使用 `pytest`
- 优先为业务规则、边界条件、状态流转补测试
- 遵循 AAA：Arrange、Act、Assert
- 测试名使用清晰描述，例如 `test_build_session_seals_inactive_session`
- 涉及外部模型、Webhook、文件系统时优先使用 mock 或夹具隔离

### 9.3 改动后的最低验证建议

- 改动后端业务逻辑：至少运行相关 `pytest`
- 改动 Python 代码：至少运行 `ruff check .`
- 改动类型复杂的模块：必要时运行 `mypy .`
- 改动数据库模型或迁移：至少验证 `alembic upgrade head`
- 改动前端页面或接口联动：至少确认构建或本地页面可用

## 10. 运行与配置注意事项

关键环境变量：

- `DATABASE_URL`
- `REDIS_URL`
- `SECRET_KEY`
- `VIDEO_ROOT_PATH`
- `PLAYBACK_CACHE_ROOT`
- `MCP_TOKEN`
- `DEFAULT_ADMIN_USERNAME`
- `DEFAULT_ADMIN_PASSWORD`
- `DB_INIT_MAX_RETRIES`
- `DB_INIT_RETRY_INTERVAL_SECONDS`
- `ANALYZER_LLM_CHUNK_SECONDS`（默认 300；每个 LLM 调用的 sub-chunk 时长）
- `ANALYZER_VIDEO_KEYFRAME_PERIOD_SECONDS`（默认 8）
- `ANALYZER_VIDEO_KEYFRAME_MAD_THRESHOLD`（默认 1.0）
- `ANALYZER_VIDEO_KEYFRAME_PHASH_THRESHOLD`（默认 6）
- `ANALYZER_VIDEO_KEYFRAME_FALLBACK_TO_MP4`（默认 true）

注意事项：

- 应用启动时会自动执行 Alembic 迁移并确保默认管理员存在
- 本地开发后端时，需要自行准备 PostgreSQL 与 Redis
- `.env.example`、`src/core/config.py`、`docker-compose.yml` 应保持理解一致，运行行为以实际代码和部署配置为准

## 11. Agent 工作准则

- 接到任务后，先阅读相关代码，再动手修改
- 优先阅读与任务直接相关的模块，不进行无关大范围扫描
- 修改时尽量复用已有服务、Schema、工具函数和任务编排
- 不要凭空假设不存在的模型字段、接口返回或状态枚举
- 如果文档与代码不一致，以代码实际实现为准，并在必要时同步文档
- 除非任务明确要求，否则不要随意调整目录结构、重命名大范围模块或替换技术栈

## 12. 提交前检查清单

提交或交付前，至少确认：

- 相关改动与当前架构一致
- 无明显破坏扫描、分析、日报、问答主链路的风险
- 新增代码符合命名、导入、类型、日志规范
- 已运行与改动相关的检查或测试
- 文档变更与代码行为保持一致

## 13. 参考文档

- 根目录 `README.md`：项目介绍、部署方式、接口概览
- 根目录 `ARCHITECTURE.md`：完整架构、模块职责、业务流程、运行机制

如需理解系统全貌，优先结合 `README.md` 与 `ARCHITECTURE.md` 一起阅读。

## 13. 项目管理

- todo文件写在 `docs\todo.md` 中。该todo需要按照前端、后端、部署、工程治理、等已有标题分类。按照时间整理已经完成的todo。程序员会在todo中添加新得todo和记录思维。每次开始执行todo时，需要先进行plan，与程序员确认执行细节之后，再进行开发。
