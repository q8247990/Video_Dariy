"""Stage-local constants for the session-build pipeline.

These constants are the externally-observable knobs of the
pipeline; the session-build runner (:mod:`src.services.session_build.runner`)
and the Celery orchestration (:mod:`src.tasks._session_build_orchestration`)
both import from here so a single change in this module flips
every consumer in lockstep.

History
=======

* ``MERGE_GAP_SECONDS`` — kept at ``1`` so two consecutive Xiaomi
  recordings whose on-camera timestamps differ by one wall-clock
  second stay inside the same ``VideoSession``. The file interval
  is ``60`` seconds, so the ``1``-second buffer swallows sub-second
  jitter without ever merging two distinct recordings.
* ``SEAL_BUFFER_SECONDS`` — kept at ``600`` so the latest
  ``OPEN`` session is sealed once no new file has appended for ten
  minutes. The hourly maintenance heartbeat is the only other
  path that can seal a session; reducing this constant would
  shorten the hot→analyze latency.
"""

from __future__ import annotations

#: Maximum gap (seconds) between two consecutive files inside the
#: same VideoSession. See module docstring.
MERGE_GAP_SECONDS: int = 1

#: Stale-session seal buffer (seconds). The latest ``OPEN`` session
#: is sealed once ``now - session_end_time`` exceeds this constant.
SEAL_BUFFER_SECONDS: int = 600

#: Batch size for the existing-hash lookup; keeps the query's bound
#: parameter list small.
HASH_QUERY_CHUNK_SIZE: int = 500

__all__ = [
    "HASH_QUERY_CHUNK_SIZE",
    "MERGE_GAP_SECONDS",
    "SEAL_BUFFER_SECONDS",
]
