"""Cross-layer webhook contract constants.

The event name and signature version are public product contracts: they
appear in subscriber filters and outbound payloads. Defining them once
prevents the producer (summary publication / analysis) and the consumer
subscription filter from drifting apart.
"""

from __future__ import annotations

from typing import Final

WEBHOOK_SIGNATURE_VERSION: Final = "1.0"

WEBHOOK_EVENT_DAILY_SUMMARY_GENERATED: Final = "daily_summary_generated"

__all__ = [
    "WEBHOOK_EVENT_DAILY_SUMMARY_GENERATED",
    "WEBHOOK_SIGNATURE_VERSION",
]
