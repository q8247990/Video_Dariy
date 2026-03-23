# MCP 快速接入（AstrBot Streamable HTTP）

本文对应当前系统的标准 MCP 接入方式。
现在不再提供旧的 REST MCP 接口，统一改为单端点 `Streamable HTTP`：`/mcp`。

## 1. 前置条件

1. 外部访问入口为前端 nginx，而不是后端容器直连
2. 前端访问地址示例：`http://127.0.0.1:8102`
3. 管理后台 -> 系统配置中：
   - 打开 `启用 MCP 接口`
   - 设置 `MCP Token`
4. AstrBot 能访问前端暴露地址

## 2. 对外入口

- MCP URL：`http://127.0.0.1:8102/mcp`
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
      "url": "http://127.0.0.1:8102/mcp",
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
- URL: `http://127.0.0.1:8102/mcp`
- Header 1: `X-MCP-Token: <你的MCPToken>`
- Header 2: `X-MCP-Source: astrbot`

## 4. 当前暴露的 MCP Tools

- `get_daily_summary`：获取某日结构化日报
- `search_events`：按时间、摄像头、关键词、标签检索事件
- `get_event_detail`：查询单个事件详情及关联 session
- `get_video_segments`：查询事件或 session 对应的视频片段
- `ask_home_monitor`：对家庭监控数据进行自然语言问答

这些 tools 由 MCP `tools/list` 自动发现，AstrBot 不需要手动逐个配置 URL。

## 5. 返回特征

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
        "text": "{\"date\":\"2026-02-26\",\"event_count\":1}"
      }
    ],
    "structuredContent": {
      "date": "2026-02-26",
      "event_count": 1
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

## 6. 连通性自检

可以先用标准 MCP 初始化请求检查链路是否畅通：

```bash
curl -sS -X POST "http://127.0.0.1:8102/mcp" \
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

## 7. Docker / 部署注意事项

- 如果 AstrBot 跑在 Docker 中，`127.0.0.1` 指向 AstrBot 自己，不是当前系统
- 应填写前端 nginx 对外地址，例如：
  - 同机映射端口：`http://宿主机IP:8102/mcp`
  - 反向代理域名：`https://your-domain/mcp`
- 不要让 AstrBot 直接连接 `backend:8000` 或宿主机 `8000`

## 8. 常见问题

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

深入排障见：`docs/develop/mcp_debug.md`
