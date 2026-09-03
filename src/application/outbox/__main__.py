"""``python -m src.application.outbox`` entry point.

Re-exports :func:`src.application.outbox.cli.main` so the publisher
CLI is invocable as a module without requiring
``python -m src.application.outbox.cli`` to be the only supported
shape. ``main`` accepts an ``argv`` override so unit / integration
tests can drive the CLI without spawning a subprocess.
"""

from __future__ import annotations

from src.application.outbox.cli import main

__all__ = ["main"]

if __name__ == "__main__":
    raise SystemExit(main())
