"""Outbox error hierarchy.

All outbox exceptions inherit from :class:`OutboxError`, which keeps
the application's ``except`` clauses clean: a use case can either
catch the base class to handle any outbox failure uniformly, or catch
the concrete subclass for a more specific response. Each concrete
error carries a ``payload`` keyword argument so the caller can include
the offending value without leaking secrets into tracebacks.
"""

from __future__ import annotations

from typing import Any, Optional


class OutboxError(Exception):
    """Base class for every outbox contract violation.

    Catch this in API endpoints / use cases when you want a uniform
    "this dispatch was refused" path that maps to a 400-class
    response. Concrete subclasses below carry the same ``payload``
    attribute; downstream code should never ``str(exc)`` the payload
    because it may contain caller-supplied data.
    """

    def __init__(self, message: str, *, payload: Optional[Any] = None) -> None:
        super().__init__(message)
        self.payload = payload


class OutboxStateError(OutboxError):
    """Raised when a state transition is not in the allowed set.

    The state machine is documented in §3 of
    ``docs/adr/0011-transactional-outbox-and-task-lifecycle.md``.
    The publisher loop and the consumer-side bind helper translate
    this exception into "the row is already terminal; skip" rather
    than "the caller's transition was illegal". The mgmt API path
    translates it into HTTP 409 Conflict.
    """


class OutboxPayloadError(OutboxError):
    """Raised when an outbox payload fails the JSON-safety rules.

    The rules live in :mod:`src.application.outbox.payload_validator`.
    The exception message always identifies the field path that
    triggered the rejection (``$``, ``$.args[2]``, ``$.kwargs.session_id``
    …) so operators can find the offending caller without a debugger.
    """


class OutboxContractViolation(OutboxError):
    """Raised when an emit-time contract rule is broken.

    Used for:
    - duplicate ``event_id`` at the contract layer (early reject;
      the DB unique constraint is the source of truth);
    - direct manipulation of an immutable :class:`OutboxEvent`
      attribute (the dataclass is ``frozen=True``);
    - any rule that lives in this module but is not strictly a
      state, payload, or registry issue.
    """


class OutboxRegistryError(OutboxContractViolation):
    """Raised when an emit / claim references an unknown task name.

    The registry whitelist lives in
    :mod:`src.application.outbox.registry`. This is a thin alias of
    :class:`OutboxContractViolation`; it exists so the API can
    distinguish "you tried to publish a command for a Celery task we
    do not know" from "you tried to emit two events with the same
    ``event_id``" and surface different error codes.
    """


__all__ = [
    "OutboxContractViolation",
    "OutboxError",
    "OutboxPayloadError",
    "OutboxRegistryError",
    "OutboxStateError",
]
