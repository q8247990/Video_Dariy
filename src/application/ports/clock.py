"""Clock and identifier ports for the application composition root.

These protocols define the abstract time and ID generation surfaces the
application use cases depend on. They exist so that:

* ``bootstrap_production()`` can bind wall-clock / ``uuid.uuid4`` adapters
  without coupling any use case to concrete helpers;
* ``bootstrap_for_tests()`` can substitute deterministic fakes that produce
  reproducible timestamps and IDs in unit tests.

Concrete implementations live under ``src/application/bootstrap.py``
(production binding) and ``src/application/bootstrap_fakes.py`` (in-memory
test doubles). Use cases must only depend on the ``Port`` types defined
here; they must not import :mod:`datetime` directly for "now" or
:mod:`uuid` for ID generation. UTC is the canonical storage zone for the
project; the production adapter returns timezone-aware UTC datetimes.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable


@runtime_checkable
class ClockPort(Protocol):
    """Abstract time source.

    The contract is intentionally minimal: a single ``now()`` accessor that
    returns a timezone-aware UTC ``datetime``. Use cases consume it for
    everything they would otherwise fetch from :func:`datetime.utcnow` or
    :func:`datetime.now`, so tests can freeze and advance time without
    monkeypatching the stdlib.
    """

    def now(self) -> datetime:
        """Return the current instant as a timezone-aware UTC ``datetime``."""
        ...


@runtime_checkable
class IdGeneratorPort(Protocol):
    """Abstract identifier generator.

    Returns a string identifier suitable for use as a Celery ``task_id``,
    correlation ID, or any other opaque application-level handle. The
    production binding uses :func:`uuid.uuid4`; tests can substitute a
    deterministic counter or seeded sequence.
    """

    def new_id(self) -> str:
        """Return a fresh, globally-unique identifier as ``str``."""
        ...


__all__ = ["ClockPort", "IdGeneratorPort"]
