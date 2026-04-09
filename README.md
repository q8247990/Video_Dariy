> **English version**: [README.en.md](README.en.md)

# Video Diary — 家庭监控视频 AI 分析系统

> 家里装了摄像头，NAS 里攒了一堆录像，但从来没人看？
>
> Video Diary 帮你自动看完每一段录像，告诉你今天家里发生了什么。

![仪表盘](image.png)

![事件与回放](image-1.png)

---

## 它能做什么

- **自动扫描录像** — 指向你的 NAS 视频目录，系统自动发现新录像、去重入库、按时间合并为完整事件
- **AI 识别关键事件** — 调用本地或云端多模态大模型，识别"猫跳上桌子"、"快递员来访"、"孩子放学回家"等场景
- **每日家庭日报** — 每天自动生成一份日报，汇总当天所有事件，提炼重点对象和关注事项
- **自然语言问答** — 直接问"昨天晚上门口有没有人停留超过 5 分钟？"，系统检索事件并回答
- **Webhook / MCP 对外能力** — 接入微信 Bot、Home Assistant、Claude Desktop 等外部系统
- **Web 管理后台** — 视频源、事件回放、日报、家庭画像、模型配置，全部可视化管理

## 快速开始

**前置条件**：Docker + Docker Compose

```bash
# 1. 克隆项目
git clone <repo-url> && cd video_dairy

# 2. 修改视频目录挂载（docker-compose.yml 顶部）
#    将 ./xiaomi_video 替换为你的摄像头录像目录

# 3. 启动
docker compose up --build -d

# 4. 打开浏览器
#    http://localhost:8226
```

默认管理员账号通过环境变量配置（见下方"配置参考"）。启动后进入引导流程，配置视频源和模型连接即可开始使用。

## 本地部署 LLM

系统兼容所有 OpenAI API 格式的模型服务。推荐使用 vLLM 在本地部署多模态大模型，完全离线运行，数据不出局域网。

### 硬件要求

| 显卡 | 显存 | 推荐模型 |
|------|------|----------|
| RTX 3090 / 3090 Ti | 24GB | MiniCPM-V 4.5 int4、MiniCPM-o 4.5 AWQ |
| RTX 4090 | 24GB | 同上，或 Qwen3.5-9B |
| 双卡 / 更高显存 | 48GB+ | 可尝试更大参数模型 |

### 推荐模型

| 模型 | 用途 | 链接 |
|------|------|------|
| MiniCPM-V 4.5 int4 | 视频理解（主力推荐） | [ModelScope](https://modelscope.cn/models/OpenBMB/MiniCPM-V-4_5-int4) |
| MiniCPM-o 4.5 AWQ | 视频理解（备选） | [ModelScope](https://modelscope.cn/models/OpenBMB/MiniCPM-o-4_5-awq) |
| Qwen3.5-9B | 文本摘要 / 日报生成 | [ModelScope](https://modelscope.cn/models/Qwen/Qwen3.5-9B) |

### vLLM 启动示例

```bash
# 安装 vLLM
pip install vllm

# 启动视觉模型（以 MiniCPM-V 4.5 int4 为例）
vllm serve OpenBMB/MiniCPM-V-4_5-int4 \
  --trust-remote-code \
  --port 8000 \
  --max-model-len 4096
```

启动后，在系统"设置 → 模型连接"中填入：

- API 地址：`http://<你的IP>:8000/v1`
- 模型名称：与 vLLM 启动时一致

### 已知限制

- **Ollama**：不支持 video 参数，无法用于视频分析
- **百炼平台**：上传超过 1 分钟的视频需要公网可访问 URL，当前不支持

## 部署方式

### Docker Compose 全栈部署（推荐）

`docker-compose.yml` 包含以下服务：

| 服务 | 说明 |
|------|------|
| postgres | 业务数据库 |
| redis | Celery 消息队列 |
| backend | FastAPI 后端 |
| celery_worker | 异步任务执行 |
| celery_beat | 定时任务调度 |
| frontend | React 前端 + Nginx 反代 |

```bash
docker compose up --build -d    # 启动
docker compose ps               # 查看状态
docker compose logs -f backend   # 查看后端日志
docker compose down              # 停止
```

### 离线交付打包

适合 NAS / 内网环境，构建镜像并导出交付包：

```bash
bash scripts/package_release.sh --tag v1.0.0
```

输出位于 `output/<tag>/`，包含镜像包和精简版 `docker-compose.yml`，用户只需修改一处视频目录挂载即可运行。

### 配置参考

| 变量 | 说明 | 是否必改 |
|------|------|----------|
| `VIDEO_ROOT_PATH` | 视频录像目录（容器内路径） | 通过 docker-compose 挂载 |
| `SECRET_KEY` | JWT 签名密钥 | 生产环境必改 |
| `DEFAULT_ADMIN_USERNAME` | 默认管理员用户名 | 建议修改 |
| `DEFAULT_ADMIN_PASSWORD` | 默认管理员密码 | 建议修改 |
| `DATABASE_URL` | PostgreSQL 连接串 | Docker 部署保持默认即可 |
| `REDIS_URL` | Redis 连接串 | Docker 部署保持默认即可 |
| `PLAYBACK_CACHE_ROOT` | HLS 播放缓存目录 | Docker 部署保持默认即可 |
| `MCP_TOKEN` | MCP 接口鉴权 Token | 需要 MCP 时配置 |

### 默认访问地址

- 前端：`http://localhost:8226`
- 健康检查：`http://localhost:8226/health`
- MCP 入口：`http://localhost:8226/mcp`

---

<details>
<summary><strong>开发者参考</strong></summary>

### 技术栈

**后端**：FastAPI / Celery / SQLAlchemy + Alembic / PostgreSQL / Redis / httpx / ffmpeg

**前端**：React 19 + TypeScript + Vite / React Router / @tanstack/react-query / Zustand / hls.js / Nginx

### 核心链路

```text
视频目录 → VideoSource → VideoFile → VideoSession → EventRecord → DailySummary
                                                  → Chat / MCP / Webhook
```

1. `heartbeat` 每 60 秒为启用中的视频源派发热扫描
2. 扫描目录、解析时间、按文件哈希去重写入 `video_file`
3. 连续片段按时间间隔合并为 `video_session`
4. 封口后的 Session 派发 AI 分析，生成 `event_record`
5. 定时或手动生成 `daily_summary`
6. 前端、问答、MCP、Webhook 基于结构化结果提供能力

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
├── docker-compose.yml
└── Dockerfile
```

更完整的架构说明见 [ARCHITECTURE.md](ARCHITECTURE.md)。

### 本地开发

```bash
# 后端（需自备 PostgreSQL + Redis）
pip install -r requirements.txt
python -m src.main

# 前端
cd frontend
npm ci
npm run dev
```

Python 版本：3.10

### 关键接口

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/v1/auth/login` | 登录 |
| GET | `/api/v1/dashboard/overview` | 仪表盘统计 |
| GET | `/api/v1/video-sources` | 视频源列表 |
| POST | `/api/v1/tasks/{id}/build/full` | 全量构建 |
| POST | `/api/v1/tasks/analyze/{session_id}` | 手动分析 |
| POST | `/api/v1/tasks/summarize` | 手动生成日报 |
| POST | `/api/v1/chat/ask` | 自然语言问答 |
| GET | `/api/v1/daily-summaries` | 日报列表 |
| GET | `/api/v1/events` | 事件列表 |
| POST | `/mcp` | MCP JSON-RPC 入口 |

### 常用命令

```bash
# 重置扫描/Session/事件/任务数据
docker compose exec backend python -m src.reset_pipeline_data

# 测试
pytest -v

# 代码检查
ruff check .
ruff format .
mypy .
```

</details>

---

<details>
<summary><strong>更多截图</strong></summary>

![宠物档案](image-2.png)

![问答页面](image-3.png)

![家庭画像](image-4.png)

</details>
