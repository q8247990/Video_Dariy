"""轻量 i18n 基座: t(key, locale, **params) + locale 资源文件加载。"""

import json
import logging
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_LOCALE = "zh-CN"
SUPPORTED_LOCALES = ("zh-CN", "en-US")

_LOCALE_DIR = Path(__file__).parent / "locales"

_LOCALE_FILE_MAP = {
    "zh-CN": "zh_CN",
    "en-US": "en_US",
}

_catalogs: dict[str, dict[str, str]] = {}

_system_default_locale: Optional[str] = None
_system_default_locale_ts: float = 0.0
_SYSTEM_LOCALE_TTL = 60.0


def _load_catalog(locale: str) -> dict[str, str]:
    stem = _LOCALE_FILE_MAP.get(locale)
    if stem is None:
        return {}
    path = _LOCALE_DIR / f"{stem}.json"
    if not path.exists():
        logger.warning("Locale file not found: %s", path)
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        logger.exception("Failed to load locale file: %s", path)
        return {}


def _get_catalog(locale: str) -> dict[str, str]:
    if locale not in _catalogs:
        _catalogs[locale] = _load_catalog(locale)
    return _catalogs[locale]


def t(key: str, locale: Optional[str] = None, **params: object) -> str:
    """t(key, locale, **params) -> 翻译字符串。找不到 key 时回退默认语言，仍无则返回 key。"""
    resolved_locale = locale if locale in SUPPORTED_LOCALES else DEFAULT_LOCALE

    catalog = _get_catalog(resolved_locale)
    template = catalog.get(key)

    if template is None and resolved_locale != DEFAULT_LOCALE:
        catalog = _get_catalog(DEFAULT_LOCALE)
        template = catalog.get(key)

    if template is None:
        return key

    if params:
        try:
            return template.format(**params)
        except (KeyError, IndexError):
            return template

    return template


def normalize_locale(raw: Optional[str]) -> str:
    """标准化 locale 输入 (zh/zh_CN/zh-CN -> zh-CN, en/en_US/en-US -> en-US)。"""
    if not raw or not raw.strip():
        return DEFAULT_LOCALE

    value = raw.strip().replace("_", "-")

    for loc in SUPPORTED_LOCALES:
        if value.lower() == loc.lower():
            return loc

    prefix = value.split("-")[0].lower()
    prefix_map = {"zh": "zh-CN", "en": "en-US"}
    return prefix_map.get(prefix, DEFAULT_LOCALE)


def get_system_default_locale() -> str:
    global _system_default_locale, _system_default_locale_ts
    now = time.monotonic()
    if (
        _system_default_locale is not None
        and (now - _system_default_locale_ts) < _SYSTEM_LOCALE_TTL
    ):
        return _system_default_locale
    try:
        from src.db.session import SessionLocal
        from src.models.system_config import SystemConfig

        db = SessionLocal()
        try:
            row = db.query(SystemConfig).filter(SystemConfig.config_key == "default_locale").first()
            if row and row.config_value:
                _system_default_locale = normalize_locale(str(row.config_value))
            else:
                _system_default_locale = DEFAULT_LOCALE
        finally:
            db.close()
    except Exception:
        _system_default_locale = DEFAULT_LOCALE
    _system_default_locale_ts = now
    return _system_default_locale


def reload_catalogs() -> None:
    global _system_default_locale, _system_default_locale_ts
    _catalogs.clear()
    _system_default_locale = None
    _system_default_locale_ts = 0.0
