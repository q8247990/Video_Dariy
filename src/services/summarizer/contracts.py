"""Daily-summary tunables and webhook event names."""

from __future__ import annotations

from src.core.contracts import (
    WEBHOOK_EVENT_DAILY_SUMMARY_GENERATED as _WEBHOOK_EVENT_DAILY_SUMMARY_GENERATED,
)

# --- constants.py ---
WEBHOOK_EVENT_DAILY_SUMMARY_GENERATED: str = _WEBHOOK_EVENT_DAILY_SUMMARY_GENERATED


# --- constants.py ---
DISPATCH_GUARD_STATE_KEY_PREFIX: str = "daily_summary_dispatch_guard"


# --- constants.py ---
SERIAL_SPLIT_PROMPT_THRESHOLD: int = 28000


__all__ = [
    "DISPATCH_GUARD_STATE_KEY_PREFIX",
    "SERIAL_SPLIT_PROMPT_THRESHOLD",
    "WEBHOOK_EVENT_DAILY_SUMMARY_GENERATED",
]
