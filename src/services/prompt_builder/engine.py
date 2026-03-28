"""Jinja2 模板渲染引擎。

提供全局单例 Environment，加载 templates/ 目录下的 .j2 模板文件。
"""

import json
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, Undefined

_TEMPLATE_DIR = Path(__file__).parent / "templates"


def _compact_text(value: Any) -> str:
    """将 None / 空字符串 / Undefined 统一为 '无'。"""
    if value is None or isinstance(value, Undefined):
        return "无"
    text = str(value).strip()
    return text or "无"


def _compact_list(value: list[Any] | None) -> str:
    """将列表用顿号连接，空列表返回 '无'。"""
    if not value or isinstance(value, Undefined):
        return "无"
    return "、".join(str(item) for item in value if str(item).strip()) or "无"


def _to_json(value: Any, indent: int = 2) -> str:
    """将 Python 对象序列化为 JSON 字符串。"""
    return json.dumps(value, ensure_ascii=False, indent=indent)


def _truncate_cjk(value: str, max_len: int) -> str:
    """截断字符串，超长时末尾加省略号。"""
    text = (value or "").strip()
    if len(text) <= max_len:
        return text
    return f"{text[: max_len - 1]}…"


def _build_environment() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATE_DIR)),
        undefined=Undefined,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=False,
        autoescape=False,
    )
    env.filters["compact_text"] = _compact_text
    env.filters["compact_list"] = _compact_list
    env.filters["to_json"] = _to_json
    env.filters["truncate_cjk"] = _truncate_cjk
    return env


_env: Environment | None = None


def _get_env() -> Environment:
    global _env
    if _env is None:
        _env = _build_environment()
    return _env


def render_template(template_name: str, **context: Any) -> str:
    """渲染指定模板并返回字符串。"""
    env = _get_env()
    template = env.get_template(template_name)
    return template.render(**context)
