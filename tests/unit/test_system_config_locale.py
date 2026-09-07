"""Tests for ``src.application.system_config`` locale providers.

These tests lock the boundary contract enforced by the architecture
boundary test (Todo 8): ``src/core/i18n`` does **not** import
``src.db.session``; instead it delegates the system-default locale
lookup to a ``LocaleProvider`` Protocol. This module exercises both
the production-shaped DB-backed provider and the in-memory fakes used
by tests.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.application.system_config import (
    CallableLocaleProvider,
    LocaleProvider,
    StaticLocaleProvider,
    SystemConfigLocaleProvider,
)
from src.core.i18n import (
    DEFAULT_LOCALE,
    SUPPORTED_LOCALES,
    get_locale_provider,
    get_system_default_locale,
    reload_catalogs,
    reset_locale_provider,
    set_locale_provider,
)

# ---------------------------------------------------------------------------
# Protocol / runtime_checkable sanity
# ---------------------------------------------------------------------------


def test_static_locale_provider_satisfies_protocol() -> None:
    provider = StaticLocaleProvider("zh-CN")
    assert isinstance(provider, LocaleProvider)


def test_system_config_locale_provider_satisfies_protocol() -> None:
    provider = SystemConfigLocaleProvider()
    assert isinstance(provider, LocaleProvider)


def test_callable_locale_provider_satisfies_protocol() -> None:
    provider = CallableLocaleProvider(lambda: "en-US")
    assert isinstance(provider, LocaleProvider)


# ---------------------------------------------------------------------------
# StaticLocaleProvider — normalisation & defaults
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("zh-CN", "zh-CN"),
        ("ZH-cn", "zh-CN"),
        ("zh_CN", "zh-CN"),
        ("zh", "zh-CN"),
        ("en-US", "en-US"),
        ("EN_US", "en-US"),
        ("en", "en-US"),
        ("fr", DEFAULT_LOCALE),
        ("", DEFAULT_LOCALE),
    ],
)
def test_static_locale_provider_normalises(raw: str, expected: str) -> None:
    provider = StaticLocaleProvider(raw)
    assert provider.get_default_locale() == expected


def test_static_locale_provider_invalidate_is_noop() -> None:
    provider = StaticLocaleProvider("en-US")
    provider.invalidate()
    assert provider.get_default_locale() == "en-US"


# ---------------------------------------------------------------------------
# CallableLocaleProvider
# ---------------------------------------------------------------------------


def test_callable_locale_provider_invokes_loader_each_time() -> None:
    calls: list[int] = []

    def _loader() -> str:
        calls.append(1)
        return "zh-CN"

    provider = CallableLocaleProvider(_loader)
    assert provider.get_default_locale() == "zh-CN"
    assert provider.get_default_locale() == "zh-CN"
    assert len(calls) == 2


# ---------------------------------------------------------------------------
# SystemConfigLocaleProvider — DB error / fallback behaviour
# ---------------------------------------------------------------------------


def test_system_config_locale_provider_returns_default_when_session_unavailable() -> None:
    """If the session factory raises (e.g. session cannot be opened), default is returned."""

    factory = MagicMock(side_effect=RuntimeError("db down"))
    provider = SystemConfigLocaleProvider(session_factory=factory)
    assert provider.get_default_locale() == DEFAULT_LOCALE


def test_system_config_locale_provider_returns_default_on_query_error() -> None:
    """DB driver errors must not bubble; the legacy contract swallows them."""

    fake_session = MagicMock()
    fake_session.query.return_value.filter.return_value.first.side_effect = RuntimeError("db down")
    factory = MagicMock(return_value=fake_session)

    provider = SystemConfigLocaleProvider(session_factory=factory)
    assert provider.get_default_locale() == DEFAULT_LOCALE


def test_system_config_locale_provider_returns_default_when_row_missing() -> None:
    """No row in ``system_config`` → DEFAULT_LOCALE."""

    fake_session = MagicMock()
    fake_session.query.return_value.filter.return_value.first.return_value = None
    factory = MagicMock(return_value=fake_session)

    provider = SystemConfigLocaleProvider(session_factory=factory)
    assert provider.get_default_locale() == DEFAULT_LOCALE


def test_system_config_locale_provider_caches_value() -> None:
    """Subsequent calls within the TTL window return the cached value."""

    fake_session = MagicMock()
    row = MagicMock()
    row.config_value = "en-US"
    fake_session.query.return_value.filter.return_value.first.return_value = row
    factory = MagicMock(return_value=fake_session)

    provider = SystemConfigLocaleProvider(ttl_seconds=60.0, session_factory=factory)
    assert provider.get_default_locale() == "en-US"
    # Second call must not re-open the session.
    assert provider.get_default_locale() == "en-US"
    assert factory.call_count == 1


def test_system_config_locale_provider_invalidate_clears_cache() -> None:
    """After ``invalidate``, the next call re-reads the DB."""

    fake_session = MagicMock()
    row = MagicMock()
    row.config_value = "en-US"
    fake_session.query.return_value.filter.return_value.first.return_value = row
    factory = MagicMock(return_value=fake_session)

    provider = SystemConfigLocaleProvider(ttl_seconds=60.0, session_factory=factory)
    provider.get_default_locale()
    provider.invalidate()
    provider.get_default_locale()
    assert factory.call_count == 2


# ---------------------------------------------------------------------------
# src.core.i18n integration — composition-root swap
# ---------------------------------------------------------------------------


def test_core_i18n_uses_active_provider() -> None:
    """``get_system_default_locale`` delegates to the bound provider."""

    saved = get_locale_provider()
    try:
        set_locale_provider(StaticLocaleProvider("en-US"))
        assert get_system_default_locale() == "en-US"
        set_locale_provider(StaticLocaleProvider("zh-CN"))
        assert get_system_default_locale() == "zh-CN"
    finally:
        set_locale_provider(saved)


def test_core_i18n_reload_catalogs_invalidates_provider() -> None:
    """``reload_catalogs`` calls ``invalidate`` on the active provider."""

    provider = MagicMock(spec=LocaleProvider)
    provider.get_default_locale.return_value = "zh-CN"
    saved = get_locale_provider()
    try:
        set_locale_provider(provider)
        reload_catalogs()
        provider.invalidate.assert_called_once()
    finally:
        set_locale_provider(saved)


def test_core_i18n_default_provider_returns_default_locale() -> None:
    """Without explicit binding, the static default kicks in."""

    saved = get_locale_provider()
    try:
        reset_locale_provider()
        assert get_system_default_locale() == DEFAULT_LOCALE
    finally:
        set_locale_provider(saved)


def test_core_i18n_does_not_import_db_session_at_module_load() -> None:
    """Regression: ``src.core.i18n`` must not import ``src.db.session``.

    If a future change reintroduces the lazy ``from src.db.session``
    import, this test fails at collection time because the import
    itself is at module scope. AST scan is the load-bearing guard —
    this test is a parallel sanity check for human readers.
    """

    import ast
    from pathlib import Path

    module_path = Path("src/core/i18n/__init__.py")
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    offenders: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "src.db.session":
            offenders.append((node.lineno, "ImportFrom src.db.session"))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "src.db.session":
                    offenders.append((node.lineno, "Import src.db.session"))
    assert not offenders, (
        "src/core/i18n/__init__.py must not import src.db.session "
        "directly — system-config provider pattern (Todo 8). Found:\n  - "
        + "\n  - ".join(f"line {ln}: {kind}" for ln, kind in offenders)
    )


def test_core_i18n_supported_locales_unchanged() -> None:
    """Sanity: SUPPORTED_LOCALES is untouched by the refactor."""

    assert set(SUPPORTED_LOCALES) == {"zh-CN", "en-US"}
