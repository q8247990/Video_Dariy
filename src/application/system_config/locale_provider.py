"""Locale provider for ``src.core.i18n``.

This module is the **application-layer** adapter that resolves the
system-wide default locale. The :class:`LocaleProvider` contract itself
now lives in :mod:`src.core.i18n.provider` so the core layer never imports
application code; application implementations (the DB-backed and callable
adapters) implement that contract here.

The module deliberately lives under ``src.application`` rather than
``src.services`` so that ``src.core.i18n`` can resolve a locale without
importing ``src.db.session`` (which would violate the
``core_no_db_session_import`` rule enforced by
``tests/architecture/test_dependency_boundaries.py``). Application-layer
code is allowed to bind ORM-backed adapters; ``src.core`` is not.

Imports from ``src.core.i18n`` happen **lazily inside the methods** to
avoid a load-time cycle.
"""

from __future__ import annotations

import time
from typing import Any, Callable

from src.core.i18n.provider import LocaleProvider, StaticLocaleProvider

# ---------------------------------------------------------------------------
# Production adapter (DB-backed)
# ---------------------------------------------------------------------------


def _fallback_locale() -> str:
    """Locale to use when the DB has no ``default_locale`` row.

    Precedence: the DB-configured value wins (handled by the caller); this
    fallback resolves from the ``DEFAULT_LOCALE`` environment variable
    (``settings.DEFAULT_LOCALE``), then the compiled i18n default.
    """
    from src.core.config import settings
    from src.core.i18n import DEFAULT_LOCALE, normalize_locale

    try:
        return normalize_locale(settings.DEFAULT_LOCALE)
    except Exception:
        return DEFAULT_LOCALE


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
        session_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._ttl_seconds = ttl_seconds
        self._cache: str | None = None
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

        fallback = _fallback_locale()

        try:
            from src.db.session import SessionLocal
            from src.models.system_config import SystemConfig
        except Exception:
            return fallback

        try:
            if self._session_factory is not None:
                db = self._session_factory()
            else:
                db = SessionLocal()
        except Exception:
            return fallback

        try:
            row = db.query(SystemConfig).filter(SystemConfig.config_key == self.CONFIG_KEY).first()
        except Exception:
            return fallback
        finally:
            try:
                db.close()
            except Exception:
                # The session may already be closed on driver errors;
                # swallow so the fallback path still runs.
                pass

        if row is None or not row.config_value:
            return fallback

        raw = row.config_value
        # Defensive: SystemConfig.config_value is typed as JSON, so the
        # configured value could in theory be a non-string. Normalise via
        # str() and then through the core i18n helper.
        from src.core.i18n import normalize_locale

        try:
            return normalize_locale(str(raw))
        except Exception:
            return fallback


# ---------------------------------------------------------------------------
# In-memory adapters
# ---------------------------------------------------------------------------


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
