"""UUID generator for outbox ``event_id`` values.

This module is intentionally thin: a single function that returns a
fresh :class:`uuid.UUID` (v4). It exists separately from
:mod:`src.application.outbox.contracts` so that:

- tests can monkeypatch ``new_event_id`` at the repository layer
  without touching the DTO / payload validator (the DTO keeps using
  :func:`src.application.outbox.contracts.new_event_id`);
- the persistence layer (Todo 12) imports a stable name that does not
  change if the DTO module evolves.

The contract module's :func:`new_event_id` and this module's
:func:`new_event_id` are intentionally the same generator under the
hood (``uuid.uuid4``); the duplication is a layering aid, not a
functional split. ``insert … returning(event_id)`` does not actually
need the application to pre-generate the UUID — the database could
emit one via ``uuid_generate_v4()`` — but pre-generating keeps the
caller in control of the value (and lets the same UUID flow back
through the publisher's ``send_task(task_id=...)`` call without an
extra round-trip).
"""

from __future__ import annotations

import uuid
from typing import Callable

#: Default generator. Tests can monkeypatch this with a deterministic
#: UUID factory (``monkeypatch.setattr(
#: "src.application.outbox.id_generator._generator", lambda:
#: uuid.UUID(int=...))``) to avoid the in-process duplicate-event-id
#: set in :mod:`src.application.outbox.contracts` getting in the way.
_generator: Callable[[], uuid.UUID] = uuid.uuid4


def new_event_id() -> uuid.UUID:
    """Return a fresh UUID v4.

    Equivalent to :func:`src.application.outbox.contracts.new_event_id`
    but importable independently. Used by the repository (Todo 12) when
    it builds an :class:`src.application.outbox.contracts.OutboxEvent`
    before INSERTing.
    """
    return _generator()


def reset_generator_for_testing() -> None:
    """Restore the default UUID v4 generator.

    Tests that monkeypatch the generator MUST call this in
    ``teardown`` so the next test gets a clean slate.
    """
    global _generator
    _generator = uuid.uuid4


def set_generator_for_testing(generator: Callable[[], uuid.UUID]) -> None:
    """Replace the generator with a deterministic test factory.

    The contract tests use this to avoid colliding with the
    in-process duplicate-event_id set in
    :mod:`src.application.outbox.contracts`. The repository integration
    tests typically do **not** need this because they round-trip the
    value through the DB and never re-emit the same UUID in the same
    process.
    """
    global _generator
    _generator = generator


__all__ = ["new_event_id", "reset_generator_for_testing", "set_generator_for_testing"]
