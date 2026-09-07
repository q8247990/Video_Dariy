"""Locale provider for ``src.core.i18n``.

This module is the **application-layer** adapter that resolves the
system-wide default locale. ``src.core.i18n.get_system_default_locale``
delegates to a :class:`LocaleProvider` Protocol instance; production code
binds :class:`SystemConfigLocaleProvider` (a thin wrapper around the
existing ``system_config.default_locale`` row), while tests can supply
in-memory fakes.

The module deliberately lives under ``src.application`` rather than
``src.services`` so that ``src.core.i18n`` can resolve a locale without
importing ``src.db.session`` (which would violate the
``core_no_db_session_import`` rule enforced by
``tests/architecture/test_dependency_boundaries.py``). Application-layer
code is allowed to bind ORM-backed adapters; ``src.core`` is not.

Imports from ``src.core.i18n`` happen **lazily inside the methods** to
avoid a load-time cycle (the i18n module imports the ``LocaleProvider``
Protocol back from this module at top level).
"""

from __future__ import annotations

import time
from typing import Any, Callable, Optional, Protocol, runtime_checkable

# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class LocaleProvider(Protocol):
    """Returns the system-wide default locale (e.g. ``"zh-CN"``).

    The locale string MUST be one of the values accepted by
    :func:`src.core.i18n.normalize_locale`. Implementations are expected
    to normalise user-supplied / DB-supplied values before returning.
    """

    def get_default_locale(self) -> str: ...

    def invalidate(self) -> None:
        """Optional: drop any cached value so the next call re-reads.

        Defined on the Protocol but ``Protocol`` doesn't enforce it at
        runtime; callers should fall back to a no-op when the bound
        provider doesn't expose ``invalidate``.
        """
        ...


# ---------------------------------------------------------------------------
# Production adapter (DB-backed)
# ---------------------------------------------------------------------------


class SystemConfigLocaleProvider(LocaleProvider):
    """Production adapter: reads ``system_config.default_locale`` with TTL cache.

    The lookup is intentionally thin — it only resolves the single config
    key the i18n module needs. Validation, alias normalisation and
    update semantics continue to live in
    :mod:`src.services.system_config_registry` so that this adapter stays
    a one-line wrapper around an existing, audited read path.

    Caching is provided here (rather than relying on the i18n module's
    module-level globals) so that the same provider instance can be
    shared by every consumer that needs the locale, and so that
    :func:`src.core.i18n.reload_catalogs` can invalidate it through the
    provider handle.

    An optional ``session_factory`` constructor argument acts as a test
    seam so unit tests can inject a fake session without monkey-patching
    ``sys.modules``.
    """

    CONFIG_KEY: str = "default_locale"
    DEFAULT_TTL_SECONDS: float = 60.0

    def __init__(
        self,
        *,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        session_factory: Optional[Callable[[], Any]] = None,
    ) -> None:
        self._ttl_seconds = ttl_seconds
        self._cache: Optional[str] = None
        self._cache_ts: float = 0.0
        self._session_factory = session_factory

    def get_default_locale(self) -> str:
        now = time.monotonic()
        if self._cache is not None and (now - self._cache_ts) < self._ttl_seconds:
            return self._cache

        resolved = self._read_from_db()
        self._cache = resolved
        self._cache_ts = now
        return resolved

    def invalidate(self) -> None:
        """Drop any cached value so the next call re-reads the database."""

        self._cache = None
        self._cache_ts = 0.0

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _read_from_db(self) -> str:
        """Open a short-lived session and read the configured locale.

        All imports are lazy so that tests using a fake provider never
        touch the SQLAlchemy session machinery. Any database / ORM error
        is swallowed and the process-wide default is returned, matching
        the legacy ``get_system_default_locale`` behaviour.
        """

        # Lazy imports to keep load order clean and to avoid forcing ORM
        # imports when a test binds a static/fake provider instead.
        from src.core.i18n import DEFAULT_LOCALE, normalize_locale

        try:
            from src.db.session import SessionLocal
            from src.models.system_config import SystemConfig
        except Exception:
            return DEFAULT_LOCALE

        try:
            if self._session_factory is not None:
                db = self._session_factory()
            else:
                db = SessionLocal()
        except Exception:
            return DEFAULT_LOCALE

        try:
            row = db.query(SystemConfig).filter(SystemConfig.config_key == self.CONFIG_KEY).first()
        except Exception:
            return DEFAULT_LOCALE
        finally:
            try:
                db.close()
            except Exception:
                # The session may already be closed on driver errors;
                # swallow so the fallback path still runs.
                pass

        if row is None or not row.config_value:
            return DEFAULT_LOCALE

        raw = row.config_value
        # Defensive: SystemConfig.config_value is typed as JSON, so the
        # configured value could in theory be a non-string. Normalise via
        # str() and then through the core i18n helper.
        try:
            return normalize_locale(str(raw))
        except Exception:
            return DEFAULT_LOCALE


# ---------------------------------------------------------------------------
# Static / in-memory adapters
# ---------------------------------------------------------------------------


class StaticLocaleProvider(LocaleProvider):
    """Deterministic provider used by tests and tool-only contexts.

    Returns the value supplied at construction time and never touches the
    database. The constructor accepts any of the supported spellings
    (``zh``, ``zh_CN``, ``zh-CN``) — normalisation happens lazily inside
    :meth:`get_default_locale` so that constructing a provider at module
    import time (before ``src.core.i18n`` has finished loading) is safe.
    """

    def __init__(self, locale: str = "zh-CN") -> None:
        # Store the raw value; defer normalisation until first use so the
        # constructor does not depend on ``src.core.i18n`` having fully
        # loaded yet (see the module docstring re: load order).
        self._raw: str = locale

    def get_default_locale(self) -> str:
        # Lazy import: only paid for once the provider is queried, never
        # at construction time.
        from src.core.i18n import SUPPORTED_LOCALES, normalize_locale

        if self._raw in SUPPORTED_LOCALES:
            return self._raw
        return normalize_locale(self._raw)

    def invalidate(self) -> None:
        # No cached state — explicit no-op so callers can invoke it
        # unconditionally without an ``isinstance`` check.
        return None


class CallableLocaleProvider(LocaleProvider):
    """Adapter that delegates to an arbitrary zero-arg callable.

    Lets test code (and ``src.core.i18n`` itself) install a stub without
    subclassing. The callable is invoked lazily on every
    :meth:`get_default_locale`; the provider does **not** cache.
    """

    def __init__(self, loader: Callable[[], str]) -> None:
        self._loader = loader

    def get_default_locale(self) -> str:
        return self._loader()

    def invalidate(self) -> None:
        return None


__all__ = [
    "CallableLocaleProvider",
    "LocaleProvider",
    "StaticLocaleProvider",
    "SystemConfigLocaleProvider",
]
