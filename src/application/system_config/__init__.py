"""Application-layer providers for system-level runtime configuration.

The system-config package hosts ``Protocol`` shapes and concrete adapters
that read runtime configuration (such as the system-wide ``default_locale``)
without forcing ``src/core`` to depend on SQLAlchemy ``Session`` or
``src/models`` ORM classes. ``src/core/i18n`` consumes the
:class:`LocaleProvider` protocol defined here; production wires
:class:`SystemConfigLocaleProvider` (the real DB-backed implementation) at
composition-root time, and tests can substitute an in-memory fake.

This package follows the application-layer rule documented in
``tests/architecture/test_dependency_boundaries.py``: it **may** import
``src.infrastructure.*`` and ``src.models.*`` (it owns the binding to the
underlying configuration storage), but it must not be imported by
``src/services/**``. Composition-root wiring lives in
``src.application.bootstrap``.
"""

from src.application.system_config.locale_provider import (
    CallableLocaleProvider,
    LocaleProvider,
    StaticLocaleProvider,
    SystemConfigLocaleProvider,
)

__all__ = [
    "CallableLocaleProvider",
    "LocaleProvider",
    "StaticLocaleProvider",
    "SystemConfigLocaleProvider",
]
