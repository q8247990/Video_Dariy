"""Celery command payload carried by a dispatch intent."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class TaskCommand:
    task_name: str
    queue: str
    args: tuple[Any, ...] = ()
    kwargs: dict[str, Any] | None = None


__all__ = ["TaskCommand"]
