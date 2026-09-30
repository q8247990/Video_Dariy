"""Shared exception base classes."""


class DomainError(ValueError):
    """Expected, client-facing validation failure raised by service layers.

    The API maps :class:`DomainError` to HTTP 400. Plain :class:`ValueError`
    is deliberately **not** mapped, so genuine programming errors surface as
    HTTP 500 instead of being masked as client input errors.
    """
