"""Failure classification helpers.

The slim Celery task uses these to decide between:

* a Celery ``self.retry`` for transient PG serialization faults
  (``40001`` / ``40P01``);
* a :class:`RecoverySignalError` for permanent failures that
  exhaust the retry budget;
* the standard ``task_log → FAILED`` + ``session → PARTIAL/FAILED``
  transition when the exception escapes the sub-chunk loop.

Centralising the detection keeps the slim task focused on Celery
plumbing.
"""

from __future__ import annotations

import logging

from sqlalchemy.exc import OperationalError

from src.services.analysis.constants import POSTGRES_RETRYABLE_SQLSTATES

logger = logging.getLogger(__name__)


def is_deadlock_operational_error(exc: BaseException) -> bool:
    """Return True if ``exc`` is a PG deadlock / serialization fault."""
    if not isinstance(exc, OperationalError):
        return False
    original = getattr(exc, "orig", None)
    if original is None:
        return False

    pgcode = str(getattr(original, "pgcode", "") or "")
    if pgcode in POSTGRES_RETRYABLE_SQLSTATES:
        return True

    message = str(original).lower()
    return "deadlock detected" in message or "could not serialize access" in message


__all__ = ["is_deadlock_operational_error"]
