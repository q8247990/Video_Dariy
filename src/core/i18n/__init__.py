"""轻量 i18n 基座: t(key, locale, **params) + locale 资源文件加载。

The system-default locale is resolved through an application-layer
:class:`~src.application.system_config.LocaleProvider` (production:
:class:`~src.application.system_config.SystemConfigLocaleProvider`,
which reads ``system_config.default_locale``). ``src.core`` never opens
a SQLAlchemy ``Session`` itself — that responsibility moved out under
Todo 8 of ``architecture-consolidation``.

Composition-root wiring lives in :mod:`src.application.bootstrap`. Tests
that need a deterministic default locale should call
:func:`set_locale_provider` (or rely on the
:class:`~src.application.system_config.StaticLocaleProvider` default
when no provider has been bound yet).
"""

import json
import logging
from pathlib import Path
from typing import Optional

from src.application.system_config import LocaleProvider, StaticLocaleProvider

logger = logging.getLogger(__name__)

DEFAULT_LOCALE = "zh-CN"
SUPPORTED_LOCALES = ("zh-CN", "en-US")

_LOCALE_DIR = Path(__file__).parent / "locales"

_LOCALE_FILE_MAP = {
    "zh-CN": "zh_CN",
    "en-US": "en_US",
}

_catalogs: dict[str, dict[str, str]] = {}

# ---------------------------------------------------------------------------
# Active LocaleProvider
# ---------------------------------------------------------------------------
#
# The active provider is the single source of truth for "what locale does
# the system fall back to when no ``Accept-Language`` / ``X-Locale`` is
# supplied?". Production binds ``SystemConfigLocaleProvider`` from
# ``src.application.bootstrap``; tests may inject their own. If no
# provider has been bound yet (e.g. during interpreter startup before
# ``src.application.bootstrap`` runs) the ``StaticLocaleProvider`` keeps
# the i18n module importable and returns ``DEFAULT_LOCALE`` so callers
# never have to special-case the unbound state.
#
_active_provider: LocaleProvider = StaticLocaleProvider(DEFAULT_LOCALE)


def get_locale_provider() -> LocaleProvider:
    """Return the currently active :class:`LocaleProvider`."""

    return _active_provider


def set_locale_provider(provider: LocaleProvider) -> None:
    """Bind a new :class:`LocaleProvider`.

    The composition root (``src.application.bootstrap``) calls this with
    a :class:`SystemConfigLocaleProvider` at boot; tests use it to swap
    in :class:`StaticLocaleProvider` /
    :class:`CallableLocaleProvider` fakes. The provider handle is the
    *only* mutable piece of state inside this module — there is no
    separate DB-backed cache here.
    """

    global _active_provider
    _active_provider = provider


def reset_locale_provider() -> None:
    """Restore the default :class:`StaticLocaleProvider`.

    Used by tests that explicitly bind a fake provider and need to
    return to a known baseline before the next test runs.
    """

    set_locale_provider(StaticLocaleProvider(DEFAULT_LOCALE))


# ---------------------------------------------------------------------------
# Catalog loading
# ---------------------------------------------------------------------------


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
            data = json.load(f)
    except Exception:
        logger.exception("Failed to load locale file: %s", path)
        return {}
    return data if isinstance(data, dict) else {}


def _get_catalog(locale: str) -> dict[str, str]:
    if locale not in _catalogs:
        _catalogs[locale] = _load_catalog(locale)
    return _catalogs[locale]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


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
    """Resolve the system-wide default locale via the active provider.

    The actual database lookup is performed inside the bound
    :class:`~src.application.system_config.LocaleProvider`
    implementation. ``src.core.i18n`` only caches for the lifetime of
    one call (the provider itself owns any TTL cache), which keeps the
    contract simple and lets the composition root control invalidation.
    """

    return _active_provider.get_default_locale()


def reload_catalogs() -> None:
    """Clear cached catalogs and any cached system default locale.

    Called by test setup / teardown hooks and after admin-driven
    configuration updates that should take effect on the next request.
    """

    _catalogs.clear()
    # Best-effort: the active provider may not expose ``invalidate`` when
    # callers bind a Protocol-typed stub; guard so reload never raises.
    invalidate = getattr(_active_provider, "invalidate", None)
    if callable(invalidate):
        try:
            invalidate()
        except Exception:
            logger.exception("LocaleProvider.invalidate() raised; ignoring")


__all__ = [
    "DEFAULT_LOCALE",
    "SUPPORTED_LOCALES",
    "get_locale_provider",
    "get_system_default_locale",
    "normalize_locale",
    "reload_catalogs",
    "reset_locale_provider",
    "set_locale_provider",
    "t",
]
