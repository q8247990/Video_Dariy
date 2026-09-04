# API 文档

## 1. 说明

本文档基于当前代码中的 FastAPI 路由整理，覆盖 HTTP API、媒体接口、健康检查与 MCP 入口。

基础约定：

- API Base Path：`/api/v1`
- 鉴权方式：除初始化管理员、登录、健康检查外，绝大多数接口使用 Bearer Token
- Token 获取：`POST /api/v1/auth/login`
- 响应包装：大多数接口返回 `BaseResponse` 或 `PaginatedResponse`

## 2. 通用响应格式

普通响应：

```json
{
  "code": 0,
  "message": "success",
  "data": {}
}
```

分页响应：

```json
{
  "code": 0,
  "message": "success",
  "data": {
    "list": [],
    "pagination": {
      "page": 1,
      "page_size": 20,
      "total": 0
    }
  }
}
```

## 3. 鉴权、OpenAPI 与运行探针

### Auth

- `POST /api/v1/auth/init`
  - 初始化管理员，仅首次有效
- `POST /api/v1/auth/login`
  - 登录，返回 JWT Token 与当前用户信息

### Health

- `GET /health`
  - 前端反代的基础健康检查，返回进程存活结果
- `GET /livez`
  - 进程存活探针，不检查外部依赖
- `GET /readyz`
  - 就绪探针；检查数据库、Redis 和当前 Alembic revision，不就绪时返回 `503`
- `GET /metrics`
  - 返回 JSON 指标：outbox 延迟/失败、heartbeat 恢复计数和分析 checkpoint 进度
- `GET /health/bootstrap`
  - 返回 Alembic 版本、已注册表数量与表名，用于启动自检
- `GET /api/v1/openapi.json`
  - FastAPI 生成的 OpenAPI 描述，是请求/响应 schema 的最终参考

## 4. 仪表盘与引导

- `GET /api/v1/dashboard/overview`
  - 获取首页概览统计
- `GET /api/v1/onboarding/status`
  - 获取引导流程状态

## 5. 视频源 API

前缀：`/api/v1/video-sources`

- `GET /`
  - 分页查询视频源
  - 支持 `enabled`、`source_type`、`keyword`
- `GET /status/batch`
  - 批量获取视频源状态，参数 `source_ids=1,2,3`
- `POST /`
  - 创建视频源
- `GET /{id}/status`
  - 获取视频源运行状态
- `PUT /{id}`
  - 更新视频源
- `DELETE /{id}`
  - 删除视频源；若存在运行中任务会拒绝
- `POST /{id}/test`
  - 校验视频源路径与可读性
- `POST /{id}/pause`
  - 暂停视频源处理
- `POST /{id}/resume`
  - 恢复视频源处理
- `POST /validate-path`
  - 独立校验目录路径

## 6. 任务 API

前缀：`/api/v1/tasks`

### 任务日志

- `GET /logs`
  - 分页查询任务日志，支持 `task_type`、`status`
- `DELETE /logs/{id}`
  - 删除已结束任务日志
- `POST /logs/{id}/stop`
  - 停止运行中/等待中的任务
- `POST /logs/{id}/retry`
  - 重试失败、超时、取消的任务

### 任务触发

- `POST /{id}/build/full`
  - 对指定视频源触发全量构建
- `POST /analyze/{session_id}`
  - 手动触发指定 Session 分析
- `POST /summarize`
  - 手动触发日报生成
  - 可传 `target_date=YYYY-MM-DD`

## 7. 会话与事件 API

### Sessions

前缀：`/api/v1/sessions`

- `GET /`
  - 分页查询 Session
- `GET /{id}/events`
  - 获取指定 Session 下的事件列表

### Events

前缀：`/api/v1/events`

- `GET /`
  - 分页查询事件
  - 支持 `source_id`、`object_type`、`action_type`、`analysis_status`
- `GET /{id}`
  - 获取事件详情，附带来源、会话和标签信息

## 8. 日报与问答 API

### Daily Summaries

前缀：`/api/v1/daily-summaries`

- `GET /`
  - 分页查询日报
- `GET /{date_str}`
  - 获取指定日期日报，日期格式 `YYYY-MM-DD`
- `POST /generate-all`
  - 按已分析完成的 Session 时间范围批量下发全部日报生成任务

### Chat

前缀：`/api/v1/chat`

- `POST /ask`
  - 提交自然语言问题
  - 返回答案、引用事件、引用 Session
- `GET /history`
  - 分页查询问答历史

## 9. 家庭画像与系统配置 API

### Home Profile

前缀：`/api/v1/home-profile`

- `GET /`
  - 获取家庭画像
- `PUT /`
  - 更新家庭画像
- `GET /entities`
  - 获取家庭成员/宠物实体列表
- `POST /entities/member`
  - 新增家庭成员
- `POST /entities/pet`
  - 新增宠物
- `PUT /entities/{entity_id}`
  - 更新实体
- `DELETE /entities/{entity_id}`
  - 逻辑删除实体
- `GET /entities/{entity_id}/image`
  - 获取实体头像文件
- `POST /entities/{entity_id}/image`
  - 上传实体头像
- `DELETE /entities/{entity_id}/image`
  - 删除实体头像
- `POST /entities/{entity_id}/generate-appearance`
  - 通过默认视觉模型生成实体外观描述
- `GET /options`
  - 获取前端可选项枚举

### System Config

前缀：`/api/v1/system-config`

- `GET /`
  - 获取系统配置
- `PUT /`
  - 更新系统配置
  - 包含日报时间、MCP Token 等

## 10. 模型与 Webhook API

### LLM Providers

前缀：`/api/v1/providers`

- `GET /`
  - 分页查询模型提供方，支持 `provider_type`、`enabled`
- `POST /`
  - 新增模型提供方
- `PUT /{id}`
  - 更新模型提供方
- `DELETE /{id}`
  - 删除模型提供方
- `GET /usage/daily`
  - 查询最近 N 天模型调用用量，`days` 最大 30
- `POST /{id}/set-default-vision`
  - 设为默认视觉模型
- `POST /{id}/set-default-qa`
  - 设为默认问答模型
- `POST /{id}/test`
  - 执行连通性、视觉和工具调用能力探测，并回写可用能力

### Webhooks

前缀：`/api/v1/webhooks`

- `GET /`
  - 分页查询 Webhook 配置
- `POST /`
  - 创建 Webhook
- `PUT /{id}`
  - 更新 Webhook
- `DELETE /{id}`
  - 删除 Webhook
- `POST /{id}/test`
  - 发送测试推送

## 11. 媒体播放 API

前缀：`/api/v1/media`

- `GET /files/{file_id}/stream`
  - 播放单个视频文件，支持 Range 请求
- `GET /sessions/{session_id}/playback`
  - 获取 Session 播放信息，包括单文件列表、合并播放地址、HLS 地址
- `GET /sessions/{session_id}/stream`
  - 播放合并后的 Session 视频
- `GET /sessions/{session_id}/hls/index.m3u8`
  - 获取 Session 的 HLS 清单
- `GET /sessions/{session_id}/hls/{file_name}`
  - 获取 HLS 媒体分片；播放 URL 必须带签名能力参数

## 12. MCP 接口

入口：`POST /mcp`

说明：

- 协议：JSON-RPC 风格
- Header：
  - `X-MCP-Token`
  - `X-MCP-Source`
  - `Mcp-Session-Id`
  - `MCP-Protocol-Version`
- 初始化方法：`initialize`
- 其他方法：`tools/list`、`tools/call`、`ping`

当前工具能力：

- `get_data_availability`
- `search_events`
- `get_sessions`
- `get_daily_summary`
- `ask_home_monitor`

## 13. 模块与路由文件映射

- `src/api/v1/endpoints/auth.py` -> 认证
- `src/api/v1/endpoints/dashboard.py` -> 仪表盘
- `src/api/v1/endpoints/video_sources.py` -> 视频源
- `src/api/v1/endpoints/tasks.py` -> 任务与任务日志
- `src/api/v1/endpoints/sessions.py` -> Session
- `src/api/v1/endpoints/events.py` -> 事件
- `src/api/v1/endpoints/daily_summaries.py` -> 日报
- `src/api/v1/endpoints/chat.py` -> 问答
- `src/api/v1/endpoints/home_profile.py` -> 家庭画像
- `src/api/v1/endpoints/system_config.py` -> 系统配置
- `src/api/v1/endpoints/llm_providers.py` -> 模型提供方
- `src/api/v1/endpoints/webhooks.py` -> Webhook
- `src/api/v1/endpoints/onboarding.py` -> 引导流程
- `src/api/v1/endpoints/media.py` -> 媒体播放
- `src/mcp/server.py` -> MCP 服务

## 14. 当前接口特点

- HTTP API 以后台管理为主，天然偏内网/受控环境
- MCP 是单独的工具调用入口，不走 `/api/v1`
- 媒体接口用于前端播放，和普通业务接口分开
- 大部分异步动作不直接执行，而是创建 `TaskLog` 和事务性 `OutboxEvent`，并返回 outbox `event_id` 形式的 Celery `task_id`
- `/health` 由前端 Nginx 反代；`/livez`、`/readyz`、`/metrics` 和 `/health/bootstrap` 是后端直连运维端点，若需从前端入口暴露，必须额外配置反代
- Webhook 写入只接受规范化的 `event_subscriptions_json`；`event_types_json` 已从应用模型移除。迁移 `20260904_0022` 为不可逆操作，生产执行前须完成线上盘点和已验证备份
