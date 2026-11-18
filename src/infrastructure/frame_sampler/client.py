"""HTTP client for the out-of-process frame sampler service.

The client POSTs a sampling request to ``{frame_sampler_url}/sample`` and
parses the 200 response into :class:`src.services.frame_sampling.FrameSet`
value types. The sampler is a single container, so the client applies three
protective behaviours:

1. **Bounded concurrency.** Each :meth:`FrameSamplerClient.sample` call
   acquires a ``threading.BoundedSemaphore`` sized by
   ``settings.queue_limit`` before touching the network. The client is
   called from synchronous Celery worker threads (one per in-flight
   analysis sub-chunk) — not from an asyncio event loop — so a
   thread-safe ``BoundedSemaphore`` is the right primitive (an
   ``asyncio.Semaphore`` would require a running loop and is not
   thread-safe anyway). ``BoundedSemaphore`` additionally fails loudly on
   over-release. The bound caps the number of concurrent sampler requests
   (including in-flight retries of the same request) so a burst of
   parallel sub-chunks cannot overwhelm the sampler's decode/compute pool;
   excess callers block instead of piling up.

2. **Retries with deterministic backoff.** 5xx responses, connection
   errors and timeouts are retried at most twice (3 attempts total) with
   fixed 0.2s / 0.4s backoff — no jitter, so behaviour is reproducible in
   tests and in the (offline) deployment. 4xx responses (400 input/param
   error, 422 undecodable video) are *never* retried: they are
   configuration/input errors that will not self-heal, and the sampler's
   ``detail`` text is surfaced verbatim (truncated to 300 chars).

3. **Strict response parsing.** A malformed 200 payload (missing keys,
   non-list shapes, invalid base64, non-ascending timestamps) raises
   :class:`SamplerUnavailable` rather than silently dropping frames: a
   partially sampled session is worse than a failed sub-chunk, which the
   checkpoint machinery can re-run.

**Lease invariant.** A sampler call is followed by the LLM vision call
inside one leased analysis sub-chunk; together they must fit inside
``ANALYSIS_LEASE_SECONDS`` (300s). The default sampler timeout
(:data:`DEFAULT_TIMEOUT_SECONDS`, 120s) therefore leaves at least 60s of
headroom: ``120 + 60 < 300``. Tests pin this inequality so a config
drift cannot silently starve the LLM call of lease time.
"""

from __future__ import annotations

import base64
import binascii
import logging
import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final

import httpx
from sqlalchemy.orm import Session

from src.db.session import task_db_session
from src.services.frame_sampling.budget import FramePlan
from src.services.frame_sampling.mode import INPUT_MODE_VIDEO, resolve_input_mode
from src.services.frame_sampling.policy import FramePolicy, parse_frame_policy
from src.services.frame_sampling.types import EventCandidate, FrameSample, FrameSet
from src.services.system_config_registry import (
    FRAME_SAMPLER_ENABLED,
    FRAME_SAMPLER_QUEUE_LIMIT,
    FRAME_SAMPLER_TIMEOUT_SECONDS,
    FRAME_SAMPLER_URL,
    get_configs,
)

logger = logging.getLogger(__name__)

#: Default ``frame_sampler_timeout_seconds`` registry value; the lease
#: invariant test pins ``DEFAULT_TIMEOUT_SECONDS + 60 < ANALYSIS_LEASE_SECONDS``.
DEFAULT_TIMEOUT_SECONDS: Final[float] = 120.0

#: Total attempts per sample call (1 initial + 2 retries).
_MAX_ATTEMPTS: Final[int] = 3

#: Deterministic backoff between attempts (no jitter), indexed by attempt-1.
_RETRY_BACKOFF_SECONDS: Final[tuple[float, ...]] = (0.2, 0.4)

#: Sampler sampling modes accepted in the request body (frozen contract).
_VALID_MODES: Final[tuple[str, ...]] = ("uniform", "dedupe", "event")

#: ``policy.preset`` -> sampler request ``mode`` (unknown preset -> event).
_MODE_BY_PRESET: Final[dict[str, str]] = {
    "baseline": "uniform",
    "dedupe": "dedupe",
    "event": "event",
}

#: Sampler 4xx detail text is surfaced truncated to this many characters.
_DETAIL_MAX_CHARS: Final[int] = 300


class SamplerDisabled(RuntimeError):
    """The frame sampler is not available for this call and no request is sent.

    Raised when the sampler feature flag is off (system config
    ``frame_sampler_enabled`` / provider policy) or the provider is in
    video input mode. Callers must treat this as a hard stop for the
    image path — there is no fallback that fabricates frames.
    """


class SamplerUnavailable(RuntimeError):
    """The sampler request could not be completed.

    Carries diagnostic context:

    - ``last_status``: last HTTP status seen (``None`` when the failure was
      a transport error, e.g. connect error / timeout, or no request was
      sent, e.g. blank URL).
    - ``attempts``: number of attempts actually made (0 for fast-fail
      cases such as a blank URL).
    """

    def __init__(self, message: str, *, last_status: int | None = None, attempts: int = 0) -> None:
        super().__init__(message)
        self.last_status = last_status
        self.attempts = attempts


@dataclass(frozen=True)
class FrameSamplerSettings:
    """Runtime settings for the frame sampler client (system-config backed)."""

    enabled: bool
    url: str
    timeout_seconds: float
    queue_limit: int


def load_frame_sampler_settings(db: Session) -> FrameSamplerSettings:
    """Read the frame sampler settings from the system-config registry.

    Defaults (when no row is stored): ``enabled=False``, ``url=""``,
    ``timeout_seconds=120``, ``queue_limit=8``.
    """
    values = get_configs(
        db,
        (
            FRAME_SAMPLER_ENABLED,
            FRAME_SAMPLER_URL,
            FRAME_SAMPLER_TIMEOUT_SECONDS,
            FRAME_SAMPLER_QUEUE_LIMIT,
        ),
    )
    return FrameSamplerSettings(
        enabled=bool(values[FRAME_SAMPLER_ENABLED]),
        url=str(values[FRAME_SAMPLER_URL]),
        timeout_seconds=float(values[FRAME_SAMPLER_TIMEOUT_SECONDS]),
        queue_limit=int(values[FRAME_SAMPLER_QUEUE_LIMIT]),
    )


FailFn = Callable[[str], SamplerUnavailable]


def _parse_frames(frames_raw: list[Any], fail: FailFn) -> tuple[FrameSample, ...]:
    """Parse the ``frames`` list; reject missing keys, bad base64, non-ascending time."""
    frames: list[FrameSample] = []
    prev_t: float | None = None
    for i, item in enumerate(frames_raw):
        if not isinstance(item, dict) or "t_seconds" not in item or "jpeg_base64" not in item:
            raise fail(f"frames[{i}] is missing 't_seconds' or 'jpeg_base64'")
        t_raw = item["t_seconds"]
        if (
            isinstance(t_raw, bool)
            or not isinstance(t_raw, (int, float))
            or not math.isfinite(float(t_raw))
            or float(t_raw) < 0
        ):
            raise fail(f"frames[{i}].t_seconds must be a non-negative finite number, got {t_raw!r}")
        t = float(t_raw)
        if prev_t is not None and t < prev_t:
            raise fail(f"frames[{i}].t_seconds={t} is not ascending (previous {prev_t})")
        b64 = item["jpeg_base64"]
        if not isinstance(b64, str):
            raise fail(f"frames[{i}].jpeg_base64 must be a base64 string, got {b64!r}")
        try:
            jpeg_bytes = base64.b64decode(b64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise fail(f"frames[{i}].jpeg_base64 is not valid base64 ({exc})") from exc
        frames.append(FrameSample(t_seconds=t, jpeg_bytes=jpeg_bytes))
        prev_t = t
    return tuple(frames)


def _parse_candidates(candidates_raw: list[Any], fail: FailFn) -> tuple[EventCandidate, ...]:
    """Parse the optional ``candidates`` list; reject non-numeric windows / bad reasons."""
    candidates: list[EventCandidate] = []
    for i, item in enumerate(candidates_raw):
        if not isinstance(item, dict):
            raise fail(f"candidates[{i}] is not an object")
        t_start = item.get("t_start")
        t_end = item.get("t_end")
        if (
            isinstance(t_start, bool)
            or isinstance(t_end, bool)
            or not isinstance(t_start, (int, float))
            or not isinstance(t_end, (int, float))
        ):
            raise fail(f"candidates[{i}] must carry numeric 't_start' and 't_end'")
        reasons = item.get("reasons", [])
        if not isinstance(reasons, list) or not all(isinstance(r, str) for r in reasons):
            raise fail(f"candidates[{i}].reasons must be a list of strings")
        candidates.append(
            EventCandidate(t_start=float(t_start), t_end=float(t_end), reasons=tuple(reasons))
        )
    return tuple(candidates)


class FrameSamplerClient:
    """httpx-based client for the frame sampler service.

    Thread-safe: a single instance may be shared by worker threads; each
    :meth:`sample` call is bounded by a ``BoundedSemaphore`` sized by
    ``concurrency`` (explicit override) or ``settings.queue_limit``.

    ``transport`` is an injection point for tests (``httpx.MockTransport``);
    in production the default network transport is used.
    """

    def __init__(
        self,
        settings: FrameSamplerSettings,
        transport: httpx.BaseTransport | None = None,
        concurrency: int | None = None,
    ) -> None:
        self._settings = settings
        self._transport = transport
        limit = concurrency if concurrency is not None else settings.queue_limit
        if limit < 1:
            raise ValueError(f"concurrency/queue_limit must be >= 1, got {limit}")
        self._semaphore = threading.BoundedSemaphore(limit)
        #: Number of attempts made by the most recent :meth:`sample` call
        #: (updated on success; failures expose the same count on the
        #: raised :class:`SamplerUnavailable`).
        self.last_attempts: int = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def sample(
        self,
        *,
        video_path: str,
        policy: FramePolicy,
        plan: FramePlan,
        mode: str,
    ) -> FrameSet:
        """Acquire one :class:`FrameSet` from the sampler for ``video_path``.

        Raises:
            SamplerDisabled: the sampler is disabled (no request sent).
            SamplerUnavailable: blank URL, exhausted retries, a 4xx
                rejection, or a malformed 200 response.
            ValueError: ``mode`` is not one of the contract modes.
        """
        if not self._settings.enabled:
            raise SamplerDisabled(
                "frame sampler is disabled; enable it via system config frame_sampler_enabled"
            )
        url = self._settings.url.strip()
        if not url:
            raise SamplerUnavailable("frame sampler URL is not configured")
        if mode not in _VALID_MODES:
            raise ValueError(f"unknown sampling mode: {mode!r}; expected one of {_VALID_MODES}")

        endpoint = url.rstrip("/") + "/sample"
        body = self._build_request_body(video_path=video_path, policy=policy, plan=plan, mode=mode)

        with self._semaphore:
            last_status: int | None = None
            for attempt in range(1, _MAX_ATTEMPTS + 1):
                try:
                    response = self._post(endpoint, body)
                except (httpx.ConnectError, httpx.TimeoutException) as exc:
                    last_status = None
                    if attempt < _MAX_ATTEMPTS:
                        logger.warning(
                            "frame sampler request failed (%s: %s), attempt %d/%d; retry in %.1fs",
                            type(exc).__name__,
                            exc,
                            attempt,
                            _MAX_ATTEMPTS,
                            _RETRY_BACKOFF_SECONDS[attempt - 1],
                        )
                        time.sleep(_RETRY_BACKOFF_SECONDS[attempt - 1])
                        continue
                    raise SamplerUnavailable(
                        f"frame sampler unreachable after {attempt} attempts "
                        f"({type(exc).__name__}: {exc})",
                        last_status=None,
                        attempts=attempt,
                    ) from exc

                if response.status_code == 200:
                    self.last_attempts = attempt
                    return self._parse_response(
                        response, status=response.status_code, attempt=attempt
                    )

                last_status = response.status_code
                if 500 <= response.status_code < 600:
                    if attempt < _MAX_ATTEMPTS:
                        logger.warning(
                            "frame sampler returned HTTP %d, attempt %d/%d; retrying in %.1fs",
                            response.status_code,
                            attempt,
                            _MAX_ATTEMPTS,
                            _RETRY_BACKOFF_SECONDS[attempt - 1],
                        )
                        time.sleep(_RETRY_BACKOFF_SECONDS[attempt - 1])
                        continue
                    raise SamplerUnavailable(
                        f"frame sampler unavailable after {attempt} attempts "
                        f"(last HTTP {last_status})",
                        last_status=last_status,
                        attempts=attempt,
                    )

                # 4xx (and any other non-200): configuration/input error,
                # never retried — surface the sampler's detail verbatim.
                detail = self._extract_detail(response)
                raise SamplerUnavailable(
                    f"frame sampler rejected the request (HTTP {last_status}): {detail}",
                    last_status=last_status,
                    attempts=attempt,
                )

        # Unreachable: the loop above always returns or raises.
        raise SamplerUnavailable(  # pragma: no cover
            "frame sampler request loop exited unexpectedly",
            last_status=last_status,
            attempts=_MAX_ATTEMPTS,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _post(self, endpoint: str, body: dict[str, Any]) -> httpx.Response:
        with httpx.Client(
            timeout=self._settings.timeout_seconds, transport=self._transport
        ) as client:
            return client.post(endpoint, json=body)

    @staticmethod
    def _build_request_body(
        *, video_path: str, policy: FramePolicy, plan: FramePlan, mode: str
    ) -> dict[str, Any]:
        """Map ``policy`` + ``plan`` + ``mode`` onto the frozen request contract.

        ``max_frames`` is the budgeted frame count (``plan.frames``); the
        plan's client-side ``timestamps`` are intentionally not sent — the
        sampler spreads its own samples across the full clip.
        """
        return {
            "video_path": video_path,
            "probe_fps": policy.probe_fps,
            "min_frames": policy.min_frames,
            "max_frames": plan.frames,
            "max_edge": policy.max_edge,
            "jpeg_quality": policy.jpeg_quality,
            "mode": mode,
            "dedupe_phash_distance": policy.dedupe_phash_distance,
            "window_pre_seconds": policy.window_pre_seconds,
            "window_post_seconds": policy.window_post_seconds,
            "max_event_windows": policy.max_event_windows,
        }

    @staticmethod
    def _extract_detail(response: httpx.Response) -> str:
        """Best-effort extraction of the sampler's 4xx ``detail`` (truncated)."""
        try:
            data = response.json()
        except (ValueError, httpx.DecodingError):
            data = None
        detail = data.get("detail") if isinstance(data, dict) else None
        if isinstance(detail, str) and detail.strip():
            return detail.strip()[:_DETAIL_MAX_CHARS]
        if detail is not None:
            return str(detail)[:_DETAIL_MAX_CHARS]
        text = response.text.strip()
        return (text or "no error detail returned")[:_DETAIL_MAX_CHARS]

    @staticmethod
    def _parse_response(response: httpx.Response, *, status: int, attempt: int) -> FrameSet:
        """Parse a 200 response into a :class:`FrameSet`; reject malformed payloads."""

        def malformed(reason: str) -> SamplerUnavailable:
            return SamplerUnavailable(
                f"frame sampler returned a malformed response: {reason}",
                last_status=status,
                attempts=attempt,
            )

        try:
            payload = response.json()
        except (ValueError, httpx.DecodingError) as exc:
            raise malformed(f"body is not valid JSON ({exc})") from exc
        if not isinstance(payload, dict):
            raise malformed("body is not a JSON object")

        frames_raw = payload.get("frames")
        if not isinstance(frames_raw, list):
            raise malformed("'frames' is missing or not a list")
        frames = _parse_frames(frames_raw, malformed)

        candidates_raw = payload.get("candidates", [])
        if not isinstance(candidates_raw, list):
            raise malformed("'candidates' is not a list")
        candidates = _parse_candidates(candidates_raw, malformed)

        elapsed = payload.get("elapsed_ms")
        if isinstance(elapsed, bool) or not isinstance(elapsed, int):
            raise malformed(f"'elapsed_ms' must be an int, got {elapsed!r}")

        return FrameSet(
            frames=tuple(frames),
            candidates=tuple(candidates),
            source="sampler",
            elapsed_ms=int(elapsed),
        )


def build_frame_set(
    provider: Any, video_path: str, plan: FramePlan, *, db: Session | None = None
) -> FrameSet:
    """Production port: acquire a :class:`FrameSet` for one analysis sub-chunk.

    This is the exact callable the analysis port (plan todo 7) wires in:

    1. resolve the provider's vision input mode — video-mode providers
       (no ``frame_sampling`` config) raise :class:`SamplerDisabled`;
    2. resolve the sampling policy and derive the sampler mode from the
       preset (``baseline`` -> ``uniform``, ``dedupe`` -> ``dedupe``,
       anything else -> ``event``);
    3. load the sampler settings from the system-config registry (using
       ``db`` when provided, otherwise a fresh :func:`task_db_session`,
       the same pattern the Celery task wrappers use);
    4. call the sampler client and return the parsed :class:`FrameSet`.
    """
    policy = parse_frame_policy(provider)
    if resolve_input_mode(provider) == INPUT_MODE_VIDEO:
        raise SamplerDisabled("provider is in video input mode")
    sample_mode = _MODE_BY_PRESET.get(policy.preset, "event")

    if db is not None:
        settings = load_frame_sampler_settings(db)
    else:
        with task_db_session() as session:
            settings = load_frame_sampler_settings(session)

    client = FrameSamplerClient(settings)
    return client.sample(video_path=video_path, policy=policy, plan=plan, mode=sample_mode)
