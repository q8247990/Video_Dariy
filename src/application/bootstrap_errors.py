"""Bootstrap errors raised by ``src.application.bootstrap``.

Centralised here so that both the production entry point and the
``bootstrap_for_tests`` override helpers share the same exception type
without creating an import cycle through :mod:`src.application.bootstrap`.
"""

from __future__ import annotations


class ConfigurationError(RuntimeError):
    """Raised when the composition root cannot build a valid container.

    Use cases:

    * ``bootstrap_production()`` discovers that a required adapter
      dependency is missing or incompatible (e.g. a stale module that
      no longer satisfies the port's ``Protocol`` shape).
    * ``bootstrap_for_tests(overrides=...)`` receives an override whose
      value does not satisfy the corresponding port's ``Protocol``.
    """

    def __init__(self, message: str, *, binding: str | None = None) -> None:
        super().__init__(message)
        self.binding = binding


__all__ = ["ConfigurationError"]
