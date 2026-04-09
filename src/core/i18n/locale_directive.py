"""共用语言指令片段。

为 LLM prompt 提供统一的语言控制指令，根据 locale 返回对应的语言要求文本。
仅影响新生成内容，不回填历史字段。
"""

from src.core.i18n import DEFAULT_LOCALE, SUPPORTED_LOCALES

# ---------------------------------------------------------------------------
# Prompt 语言指令：注入到 system prompt 中，控制 LLM 输出语言
# ---------------------------------------------------------------------------

_LANGUAGE_DIRECTIVES: dict[str, str] = {
    "zh-CN": "请使用简体中文回答。",
    "en-US": "Please respond in English.",
}

# ---------------------------------------------------------------------------
# Prompt 语气指令：注入到 QA 类 system prompt 中，控制回答风格
# ---------------------------------------------------------------------------

_TONE_DIRECTIVES: dict[str, str] = {
    "zh-CN": "语气自然亲切，像家人之间的对话。",
    "en-US": "Use a warm, natural tone, like a conversation between family members.",
}

# ---------------------------------------------------------------------------
# 日报 fallback 文案
# ---------------------------------------------------------------------------

_FALLBACK_SUMMARY_NO_EVENTS: dict[str, str] = {
    "zh-CN": "昨天家中整体较为平稳，未观测到明确的关键活动。",
    "en-US": "The home was generally calm yesterday with no notable activities observed.",
}

_FALLBACK_SUMMARY_HAS_EVENTS: dict[str, str] = {
    "zh-CN": "昨天家中有一定活动，系统已生成简要总结，建议查看事件明细以获取更多细节。",
    "en-US": (
        "There was some activity at home yesterday."
        " A brief summary has been generated."
        " Check event details for more information."
    ),
}

# ---------------------------------------------------------------------------
# 日报标题模板
# ---------------------------------------------------------------------------

_SUMMARY_TITLE_TEMPLATE: dict[str, str] = {
    "zh-CN": "{date} 家庭日报",
    "en-US": "{date} Daily Summary",
}

# ---------------------------------------------------------------------------
# Presenter 标签
# ---------------------------------------------------------------------------

_PRESENTER_LABELS: dict[str, dict[str, str]] = {
    "zh-CN": {
        "subject_sections_header": "对象小结：",
        "attention_items_header": "关注事项：",
        "subject_type_member": "成员",
        "subject_type_pet": "宠物",
        "unnamed_subject": "未知对象",
        "unnamed_attention": "未命名关注项",
    },
    "en-US": {
        "subject_sections_header": "Subject Summary:",
        "attention_items_header": "Attention Items:",
        "subject_type_member": "Member",
        "subject_type_pet": "Pet",
        "unnamed_subject": "Unknown Subject",
        "unnamed_attention": "Unnamed Attention Item",
    },
}

# ---------------------------------------------------------------------------
# Summarizer 内部 fallback 文案
# ---------------------------------------------------------------------------

_SUBJECT_ACTIVITY_TEMPLATE: dict[str, str] = {
    "zh-CN": "{name}当日有{count}次相关活动，整体状态平稳。",
    "en-US": "{name} had {count} related activities today, overall stable.",
}

_SUBJECT_NO_ACTIVITY_TEMPLATE: dict[str, str] = {
    "zh-CN": "{name}当日未观察到明确活动。",
    "en-US": "No clear activity observed for {name} today.",
}

_SUBJECT_FALLBACK_TEMPLATE: dict[str, str] = {
    "zh-CN": "{name}当日有{count}次相关活动，系统已提取{clusters}类关键模式。",
    "en-US": "{name} had {count} related activities today, with {clusters} key patterns extracted.",
}

# ---------------------------------------------------------------------------
# Retry prompt 片段
# ---------------------------------------------------------------------------

_RETRY_JSON_INSTRUCTION: dict[str, str] = {
    "zh-CN": "请严格只输出JSON对象。",
    "en-US": "Please output only a JSON object, strictly.",
}

_RETRY_STRUCTURED_INSTRUCTION: dict[str, str] = {
    "zh-CN": (
        "请严格输出一个 JSON 对象，禁止输出解释文字、代码块或多余片段。\n"
        "如果字段缺失，请使用空数组或简短字符串占位，保持 JSON 可解析。"
    ),
    "en-US": (
        "Please output a single JSON object strictly."
        " Do not include explanations, code blocks, or extra text.\n"
        "If fields are missing, use empty arrays or short placeholder"
        " strings to keep the JSON parseable."
    ),
}

_RETRY_SYSTEM_ROLE: dict[str, str] = {
    "zh-CN": "你负责生成结构化家庭日报。",
    "en-US": "You are responsible for generating a structured daily home summary.",
}


# ---------------------------------------------------------------------------
# MCP tool descriptions
# ---------------------------------------------------------------------------

_MCP_TOOL_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "get_data_availability": {
        "zh-CN": "查询系统中数据的时间范围和视频源列表",
        "en-US": "Query the time range and video source list of data in the system",
    },
    "search_events": {
        "zh-CN": "按时间范围和过滤条件查询事件列表",
        "en-US": "Search events by time range and filter conditions",
    },
    "get_sessions": {
        "zh-CN": "按时间范围和主体查询 session 摘要列表",
        "en-US": "Query session summaries by time range and subjects",
    },
    "get_daily_summary": {
        "zh-CN": "按日期范围查询日报",
        "en-US": "Query daily summaries by date range",
    },
    "ask_home_monitor": {
        "zh-CN": "对家庭监控数据进行自然语言问答",
        "en-US": "Ask natural language questions about home monitoring data",
    },
}

# ---------------------------------------------------------------------------
# MCP tool inputSchema field descriptions
# ---------------------------------------------------------------------------

_MCP_FIELD_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "start_time": {
        "zh-CN": "开始时间，ISO 8601 格式",
        "en-US": "Start time in ISO 8601 format",
    },
    "end_time": {
        "zh-CN": "结束时间，ISO 8601 格式",
        "en-US": "End time in ISO 8601 format",
    },
    "subjects": {
        "zh-CN": "主体名称列表",
        "en-US": "List of subject names",
    },
    "keywords": {
        "zh-CN": "关键词列表，匹配事件标题/摘要/详情",
        "en-US": "Keywords to match event title/summary/details",
    },
    "event_types": {
        "zh-CN": "事件类型列表",
        "en-US": "List of event types",
    },
    "importance_levels": {
        "zh-CN": "重要程度列表",
        "en-US": "List of importance levels",
    },
    "limit": {
        "zh-CN": "返回数量上限，默认 20",
        "en-US": "Maximum number of results, default 20",
    },
    "start_date": {
        "zh-CN": "开始日期，格式 YYYY-MM-DD",
        "en-US": "Start date in YYYY-MM-DD format",
    },
    "end_date": {
        "zh-CN": "结束日期，格式 YYYY-MM-DD",
        "en-US": "End date in YYYY-MM-DD format",
    },
    "question": {
        "zh-CN": "自然语言问题",
        "en-US": "Natural language question",
    },
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _resolve(locale: str | None) -> str:
    """将 locale 归一化为支持的值，不支持时回退默认。"""
    if locale and locale in SUPPORTED_LOCALES:
        return locale
    return DEFAULT_LOCALE


def get_language_directive(locale: str | None = None) -> str:
    """返回 prompt 语言指令片段。"""
    return _LANGUAGE_DIRECTIVES.get(_resolve(locale), _LANGUAGE_DIRECTIVES[DEFAULT_LOCALE])


def get_tone_directive(locale: str | None = None) -> str:
    """返回 prompt 语气指令片段。"""
    return _TONE_DIRECTIVES.get(_resolve(locale), _TONE_DIRECTIVES[DEFAULT_LOCALE])


def get_fallback_summary(has_events: bool, locale: str | None = None) -> str:
    """返回日报 fallback 文案。"""
    loc = _resolve(locale)
    if has_events:
        return _FALLBACK_SUMMARY_HAS_EVENTS.get(loc, _FALLBACK_SUMMARY_HAS_EVENTS[DEFAULT_LOCALE])
    return _FALLBACK_SUMMARY_NO_EVENTS.get(loc, _FALLBACK_SUMMARY_NO_EVENTS[DEFAULT_LOCALE])


def get_summary_title(target_date_str: str, locale: str | None = None) -> str:
    """返回日报标题。"""
    loc = _resolve(locale)
    tpl = _SUMMARY_TITLE_TEMPLATE.get(loc, _SUMMARY_TITLE_TEMPLATE[DEFAULT_LOCALE])
    return tpl.format(date=target_date_str)


def get_presenter_label(key: str, locale: str | None = None) -> str:
    """返回 presenter 展示标签。"""
    loc = _resolve(locale)
    labels = _PRESENTER_LABELS.get(loc, _PRESENTER_LABELS[DEFAULT_LOCALE])
    return labels.get(key, key)


def get_subject_activity_text(name: str, count: int, locale: str | None = None) -> str:
    """返回对象活动 fallback 文案。"""
    loc = _resolve(locale)
    tpl = _SUBJECT_ACTIVITY_TEMPLATE.get(loc, _SUBJECT_ACTIVITY_TEMPLATE[DEFAULT_LOCALE])
    return tpl.format(name=name, count=count)


def get_subject_no_activity_text(name: str, locale: str | None = None) -> str:
    """返回对象无活动 fallback 文案。"""
    loc = _resolve(locale)
    tpl = _SUBJECT_NO_ACTIVITY_TEMPLATE.get(loc, _SUBJECT_NO_ACTIVITY_TEMPLATE[DEFAULT_LOCALE])
    return tpl.format(name=name)


def get_subject_fallback_text(
    name: str, count: int, clusters: int, locale: str | None = None
) -> str:
    """返回对象 fallback 摘要文案。"""
    loc = _resolve(locale)
    tpl = _SUBJECT_FALLBACK_TEMPLATE.get(loc, _SUBJECT_FALLBACK_TEMPLATE[DEFAULT_LOCALE])
    return tpl.format(name=name, count=count, clusters=clusters)


def get_retry_json_instruction(locale: str | None = None) -> str:
    """返回 retry 时的 JSON 指令。"""
    loc = _resolve(locale)
    return _RETRY_JSON_INSTRUCTION.get(loc, _RETRY_JSON_INSTRUCTION[DEFAULT_LOCALE])


def get_retry_structured_instruction(locale: str | None = None) -> str:
    """返回 retry 时的结构化输出指令。"""
    loc = _resolve(locale)
    return _RETRY_STRUCTURED_INSTRUCTION.get(loc, _RETRY_STRUCTURED_INSTRUCTION[DEFAULT_LOCALE])


def get_retry_system_role(locale: str | None = None) -> str:
    """返回 retry 时的 system role。"""
    loc = _resolve(locale)
    return _RETRY_SYSTEM_ROLE.get(loc, _RETRY_SYSTEM_ROLE[DEFAULT_LOCALE])


def get_mcp_tool_description(tool_name: str, locale: str | None = None) -> str:
    """返回 MCP tool 的 description，按 locale 选择语言。"""
    loc = _resolve(locale)
    descs = _MCP_TOOL_DESCRIPTIONS.get(tool_name)
    if descs is None:
        return tool_name
    return descs.get(loc, descs.get(DEFAULT_LOCALE, tool_name))


def get_mcp_field_description(field_name: str, locale: str | None = None) -> str:
    """返回 MCP tool inputSchema 字段的 description，按 locale 选择语言。"""
    loc = _resolve(locale)
    descs = _MCP_FIELD_DESCRIPTIONS.get(field_name)
    if descs is None:
        return field_name
    return descs.get(loc, descs.get(DEFAULT_LOCALE, field_name))
