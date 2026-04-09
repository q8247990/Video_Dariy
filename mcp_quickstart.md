> **English version**: [mcp_quickstart.en.md](mcp_quickstart.en.md)

# MCP 快速接入（AstrBot Streamable HTTP）

本文对应当前系统的标准 MCP 接入方式。
现在不再提供旧的 REST MCP 接口，统一改为单端点 `Streamable HTTP`：`/mcp`。

## 1. 前置条件

1. 外部访问入口为前端 nginx，而不是后端容器直连
2. 前端访问地址示例：`http://127.0.0.1:8226`
3. 管理后台 -> 系统配置中：
   - 打开 `启用 MCP 接口`
   - 设置 `MCP Token`
4. AstrBot 能访问前端暴露地址

## 2. 对外入口

- MCP URL：`http://127.0.0.1:8226/mcp`
- 鉴权 Header：`X-MCP-Token: <你的MCPToken>`
- 可选来源 Header：`X-MCP-Source: astrbot`
- 传输方式：`streamable-http`

说明：

- 当前后端真实端口仍是 `8000`，但它不直接对外使用
- AstrBot 必须连接前端 nginx 暴露的 `/mcp`
- nginx 会将 `/mcp` 代理到后端标准 MCP 服务

## 3. AstrBot 配置模板

把下面配置中的地址和 token 改成你的实际值：

```json
{
  "mcpServers": {
    "home-monitor": {
      "transport": "streamable-http",
      "url": "http://127.0.0.1:8226/mcp",
      "headers": {
        "X-MCP-Token": "请替换为你的MCPToken",
        "X-MCP-Source": "astrbot"
      }
    }
  }
}
```

如果 AstrBot UI 使用表单方式填写，对应值如下：

- Name: `home-monitor`
- Transport: `streamable-http`
- URL: `http://127.0.0.1:8226/mcp`
- Header 1: `X-MCP-Token: <你的MCPToken>`
- Header 2: `X-MCP-Source: astrbot`

## 4. 当前暴露的 MCP Tools

当前实际暴露的 tools 以 MCP `tools/list` 返回为准，下面是主要用途说明。

### 4.1 `get_data_availability`

- 用途：查询系统中已有数据的时间范围和视频源列表
- 适合：
  - 还不确定系统里覆盖了哪些日期
  - 还不清楚有哪些视频源可查
  - 想先探测数据边界，再决定后续查询范围
- 参数：无

### 4.2 `search_events`

- 用途：按时间范围和过滤条件查询事件列表
- 适合：
  - 查询某段时间是否发生过某类事件
  - 查询某个对象、关键词、事件类型、重要程度相关事件
- 必填参数：
  - `start_time`：开始时间，ISO 8601 格式
  - `end_time`：结束时间，ISO 8601 格式
- 可选参数：
  - `subjects`：主体名称列表
  - `keywords`：关键词列表，匹配事件标题/摘要/详情
  - `event_types`：事件类型列表
  - `importance_levels`：重要程度列表，枚举为 `low` / `medium` / `high`
  - `limit`：返回数量上限，默认 20

### 4.3 `get_sessions`

- 用途：按时间范围和主体查询 session 摘要列表
- 适合：
  - 查询某段时间整体发生了什么
  - 先粗粒度浏览活动，再决定是否继续下钻到具体事件
- 必填参数：
  - `start_time`：开始时间，ISO 8601 格式
  - `end_time`：结束时间，ISO 8601 格式
- 可选参数：
  - `subjects`：主体名称列表
  - `limit`：返回数量上限，默认 20

### 4.4 `get_daily_summary`

- 用途：按日期范围查询日报
- 适合：
  - 查询某天家庭日报
  - 查询一段日期范围内的日报汇总
- 必填参数：
  - `start_date`：开始日期，格式 `YYYY-MM-DD`
- 可选参数：
  - `end_date`：结束日期，格式 `YYYY-MM-DD`

### 4.5 `ask_home_monitor`

- 用途：对家庭监控数据进行自然语言问答
- 适合：
  - 复杂自然语言问题
  - 需要系统内部问答能力自动规划检索的问题
  - 希望直接输入自然语言并获得综合回答的问题
- 必填参数：
  - `question`：自然语言问题

## 5. AstrBot 工具使用提示词（可选）

如果你希望 AstrBot 更稳定地使用这些 MCP tools，可以在 AstrBot 的系统提示词中补充下面这段：

```text
你可以通过 MCP tools 查询家庭监控分析数据。

工具使用建议：
- 查询数据范围或可用视频源：get_data_availability
- 查询具体事件：search_events
- 查询某段时间的整体活动：get_sessions
- 查询某天或某段日期的日报：get_daily_summary
- 复杂自然语言问题：ask_home_monitor

调用 ask_home_monitor 前，尽量从用户问题中整理这些信息：
- 时间范围
- 区域或摄像头
- 对象
- 行为
- 输出要求

如果用户问题已经足够明确，直接传原问题即可。
如果缺少关键条件，先追问一个最关键的问题。

可选整理格式：

查询条件：
- 时间：
- 区域：
- 对象：
- 行为：
- 输出要求：

用户问题：
<原始问题>
```

## 6. 返回特征

- 协议：JSON-RPC 2.0
- 初始化：`initialize` + `notifications/initialized`
- Session Header：`Mcp-Session-Id`
- Protocol Header：`MCP-Protocol-Version`
- Tool 调用结果使用标准 MCP `tools/call` 返回结构

一个成功的 `tools/call` 结果示例：

```json
{
  "jsonrpc": "2.0",
  "id": 3,
  "result": {
    "content": [
      {
        "type": "text",
        "text": "{\"summaries\":[{\"date\":\"2026-02-26\",\"summary_text\":\"今天家中整体平稳。\"}]}"
      }
    ],
    "structuredContent": {
      "summaries": [
        {
          "date": "2026-02-26",
          "summary_text": "今天家中整体平稳。"
        }
      ]
    },
    "isError": false
  }
}
```

一个 tool 业务错误示例：

```json
{
  "jsonrpc": "2.0",
  "id": 4,
  "result": {
    "content": [
      {
        "type": "text",
        "text": "{\"error\":{\"code\":\"INVALID_ARGUMENT\",\"message\":\"date format must be YYYY-MM-DD\"}}"
      }
    ],
    "structuredContent": {
      "error": {
        "code": "INVALID_ARGUMENT",
        "message": "date format must be YYYY-MM-DD"
      }
    },
    "isError": true
  }
}
```

## 7. 连通性自检

可以先用标准 MCP 初始化请求检查链路是否畅通：

```bash
curl -sS -X POST "http://127.0.0.1:8226/mcp" \
  -H "Content-Type: application/json" \
  -H "X-MCP-Token: YOUR_TOKEN" \
  -d '{
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
      "protocolVersion": "2025-06-18",
      "capabilities": {},
      "clientInfo": {"name": "quickstart-check", "version": "1.0"}
    }
  }'
```

如果返回包含：

- `result.serverInfo.name`
- `result.capabilities.tools`
- 响应头 `Mcp-Session-Id`

说明 MCP 入口已打通。

## 8. Docker / 部署注意事项

- 如果 AstrBot 跑在 Docker 中，`127.0.0.1` 指向 AstrBot 自己，不是当前系统
- 应填写前端 nginx 对外地址，例如：
  - 同机映射端口：`http://宿主机IP:8226/mcp`
  - 反向代理域名：`https://your-domain/mcp`
- 不要让 AstrBot 直接连接 `backend:8000` 或宿主机 `8000`

## 9. 常见问题

### Q1: AstrBot 配置了 `streamable-http` 但连不上

按顺序检查：

1. AstrBot 填的是不是前端地址 `/mcp`
2. `frontend/nginx.conf` 是否已代理 `/mcp`
3. 后台 `mcp_enabled` 是否打开
4. `X-MCP-Token` 是否正确

### Q2: 初始化成功但 tools 调用失败

检查：

1. 后续请求是否带上 `Mcp-Session-Id`
2. 后续请求是否带上 `MCP-Protocol-Version`
3. tool 参数格式是否符合 `tools/list` 返回的 `inputSchema`

### Q3: `ask_home_monitor` 返回 `INTERNAL_ERROR`

通常是 QA Provider 未配置或不可用，请确认：

1. 有 `supports_qa=true` 的 provider
2. 该 provider 已启用
3. 已设置为默认 QA provider

---

深入排障见：[docs/develop/mcp_debug.md](docs/develop/mcp_debug.md)
