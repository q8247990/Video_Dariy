> **中文版**: [mcp_quickstart.md](mcp_quickstart.md)

# MCP Quick Start (AstrBot Streamable HTTP)

This document covers the standard MCP integration method for the current system.
The legacy REST MCP interface is no longer available. All MCP access is unified through a single Streamable HTTP endpoint: `/mcp`.

## 1. Prerequisites

1. The external access entry point is the frontend Nginx, not a direct connection to the backend container
2. Frontend access URL example: `http://127.0.0.1:8226`
3. In the admin console → System Configuration:
   - Enable `MCP Interface`
   - Set the `MCP Token`
4. AstrBot must be able to reach the frontend URL

## 2. External Entry Point

- MCP URL: `http://127.0.0.1:8226/mcp`
- Auth Header: `X-MCP-Token: <your MCP token>`
- Optional source Header: `X-MCP-Source: astrbot`
- Transport: `streamable-http`

Notes:

- The backend's actual port is still `8000`, but it is not exposed directly
- AstrBot must connect to the frontend Nginx-exposed `/mcp`
- Nginx proxies `/mcp` to the backend's standard MCP service

## 3. AstrBot Configuration Template

Replace the URL and token with your actual values:

```json
{
  "mcpServers": {
    "home-monitor": {
      "transport": "streamable-http",
      "url": "http://127.0.0.1:8226/mcp",
      "headers": {
        "X-MCP-Token": "replace-with-your-mcp-token",
        "X-MCP-Source": "astrbot"
      }
    }
  }
}
```

If AstrBot UI uses a form-based configuration, the corresponding values are:

- Name: `home-monitor`
- Transport: `streamable-http`
- URL: `http://127.0.0.1:8226/mcp`
- Header 1: `X-MCP-Token: <your MCP token>`
- Header 2: `X-MCP-Source: astrbot`

## 4. Available MCP Tools

The actual exposed tools are defined by the MCP `tools/list` response. Below is a summary of the main tools and their purposes.

### 4.1 `get_data_availability`

- Purpose: Query the time range and video source list of available data in the system
- Use when:
  - You are unsure which dates the system covers
  - You want to know which video sources are available
  - You want to probe data boundaries before deciding on query ranges
- Parameters: None

### 4.2 `search_events`

- Purpose: Query event list by time range and filter conditions
- Use when:
  - Checking whether a certain type of event occurred during a time period
  - Querying events related to a specific subject, keyword, event type, or importance level
- Required parameters:
  - `start_time`: Start time, ISO 8601 format
  - `end_time`: End time, ISO 8601 format
- Optional parameters:
  - `subjects`: List of subject names
  - `keywords`: Keyword list, matches event title/summary/details
  - `event_types`: Event type list
  - `importance_levels`: Importance level list, enum values: `low` / `medium` / `high`
  - `limit`: Maximum number of results, default 20

### 4.3 `get_sessions`

- Purpose: Query session summary list by time range and subjects
- Use when:
  - Checking what happened overall during a time period
  - Browsing activity at a coarse granularity before drilling down into specific events
- Required parameters:
  - `start_time`: Start time, ISO 8601 format
  - `end_time`: End time, ISO 8601 format
- Optional parameters:
  - `subjects`: List of subject names
  - `limit`: Maximum number of results, default 20

### 4.4 `get_daily_summary`

- Purpose: Query daily reports by date range
- Use when:
  - Retrieving the family daily report for a specific day
  - Querying report summaries across a date range
- Required parameters:
  - `start_date`: Start date, format `YYYY-MM-DD`
- Optional parameters:
  - `end_date`: End date, format `YYYY-MM-DD`

### 4.5 `ask_home_monitor`

- Purpose: Natural language Q&A over home surveillance data
- Use when:
  - Asking complex natural language questions
  - Needing the system's internal Q&A capability to automatically plan retrieval
  - Wanting to input natural language and receive a comprehensive answer
- Required parameters:
  - `question`: Natural language question

## 5. AstrBot Tool Usage Prompt (Optional)

If you want AstrBot to use these MCP tools more reliably, you can add the following to AstrBot's system prompt:

```text
You can query home surveillance analysis data via MCP tools.

Tool usage guidelines:
- Query data range or available video sources: get_data_availability
- Query specific events: search_events
- Query overall activity for a time period: get_sessions
- Query daily reports for a day or date range: get_daily_summary
- Complex natural language questions: ask_home_monitor

Before calling ask_home_monitor, try to extract the following from the user's question:
- Time range
- Area or camera
- Subject
- Behavior
- Output requirements

If the user's question is already specific enough, pass it directly.
If key conditions are missing, ask one critical clarifying question first.

Optional structured format:

Query conditions:
- Time:
- Area:
- Subject:
- Behavior:
- Output requirements:

User question:
<original question>
```

## 6. Response Characteristics

- Protocol: JSON-RPC 2.0
- Initialization: `initialize` + `notifications/initialized`
- Session Header: `Mcp-Session-Id`
- Protocol Header: `MCP-Protocol-Version`
- Tool call results use the standard MCP `tools/call` response structure

Example of a successful `tools/call` result:

```json
{
  "jsonrpc": "2.0",
  "id": 3,
  "result": {
    "content": [
      {
        "type": "text",
        "text": "{\"summaries\":[{\"date\":\"2026-02-26\",\"summary_text\":\"A calm day at home overall.\"}]}"
      }
    ],
    "structuredContent": {
      "summaries": [
        {
          "date": "2026-02-26",
          "summary_text": "A calm day at home overall."
        }
      ]
    },
    "isError": false
  }
}
```

Example of a tool business error:

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

## 7. Connectivity Self-Check

You can verify the link is working with a standard MCP initialization request:

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

If the response contains:

- `result.serverInfo.name`
- `result.capabilities.tools`
- Response header `Mcp-Session-Id`

Then the MCP entry point is operational.

## 8. Docker / Deployment Notes

- If AstrBot runs inside Docker, `127.0.0.1` refers to AstrBot itself, not the host machine
- Use the frontend Nginx external address instead, for example:
  - Same-machine port mapping: `http://<host-IP>:8226/mcp`
  - Reverse proxy domain: `https://your-domain/mcp`
- Do not let AstrBot connect directly to `backend:8000` or the host's port `8000`

## 9. FAQ

### Q1: AstrBot configured `streamable-http` but cannot connect

Check in order:

1. Is AstrBot using the frontend address with `/mcp`?
2. Does `frontend/nginx.conf` proxy `/mcp`?
3. Is `mcp_enabled` turned on in the admin console?
4. Is `X-MCP-Token` correct?

### Q2: Initialization succeeds but tool calls fail

Check:

1. Are subsequent requests including the `Mcp-Session-Id` header?
2. Are subsequent requests including the `MCP-Protocol-Version` header?
3. Do tool parameters match the `inputSchema` returned by `tools/list`?

### Q3: `ask_home_monitor` returns `INTERNAL_ERROR`

This usually means the QA Provider is not configured or unavailable. Verify:

1. There is a provider with `supports_qa=true`
2. That provider is enabled
3. It is set as the default QA provider

---

For in-depth troubleshooting, see: [docs/develop/mcp_debug.en.md](docs/develop/mcp_debug.en.md)
