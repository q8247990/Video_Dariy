"""Locale-provider contract owned by the core i18n layer.

The protocol and the deterministic in-memory provider live here so that
``src.core.i18n`` never has to import the application layer. Application
adapters (DB-backed, callable) implement this protocol and are re-exported
from :mod:`src.application.system_config`.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class LocaleProvider(Protocol):
    """Return the system-wide default locale (e.g. ``"zh-CN"``)."""

    def get_default_locale(self) -> str: ...

    def invalidate(self) -> None: ...


class StaticLocaleProvider:
    """Deterministic provider used by tests and tool-only contexts."""

    def __init__(self, locale: str = "zh-CN") -> None:
        self._raw: str = locale

    def get_default_locale(self) -> str:
        from src.core.i18n import SUPPORTED_LOCALES, normalize_locale

        if self._raw in SUPPORTED_LOCALES:
            return self._raw
        return normalize_locale(self._raw)

    def invalidate(self) -> None:
        return None


__all__ = ["LocaleProvider", "StaticLocaleProvider"]
