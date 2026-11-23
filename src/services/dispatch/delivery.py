"""Dispatch intents: bind a TaskLog to one broker delivery.

The dispatcher records a :class:`~src.models.task_delivery.TaskDelivery`
row next to the ``TaskLog`` inside the caller's transaction, and the caller
flushes the recorded intents after commit. Unlike the retired outbox
publisher there is no separate process: the API request and the worker
commit-then-send it directly.
"""

from __future__ import annotations

from src.services.dispatch.outbox import (
    MAX_DELIVERY_ATTEMPTS,
    DeliveryIntent,
    flush_task_deliveries,
    new_task_id,
    record_delivery,
    replay_retired_outbox,
    retry_failed_deliveries,
    send_delivery,
    task_id_for,
)

__all__ = [
    "DeliveryIntent",
    "MAX_DELIVERY_ATTEMPTS",
    "flush_task_deliveries",
    "new_task_id",
    "record_delivery",
    "replay_retired_outbox",
    "retry_failed_deliveries",
    "send_delivery",
    "task_id_for",
]
