"""Unit tests for the frame sampler HTTP client (plan todo 20).

Covers the frozen sampler contract end-to-end against an in-process
``httpx.MockTransport`` (no network):

- the exact request body mapping from ``FramePolicy`` / ``FramePlan`` / mode;
- mode validation (all four contract modes accepted, unknown modes rejected
  with ``ValueError`` and zero HTTP calls);
- retry semantics (5xx / connect / timeout retried twice, deterministic
  backoff, ``attempts`` recorded);
- 4xx handling (never retried, sampler ``detail`` surfaced, truncated);
- disabled / unconfigured fast-fail with zero requests;
- malformed 200 payloads (missing keys, bad base64, non-ascending
  timestamps) are rejected, never silently dropped;
- legacy 200 payload keys (``candidates`` / ``duration_seconds``) are
  tolerated and ignored;
- ``build_frame_set`` port: video-mode provider raises ``SamplerDisabled``
  before any I/O, image-mode provider delegates to the client with the
  policy's sampling mode (legacy ``preset`` configs resolve to a mode);
- the timeout/lease invariant: default timeout + margin < ANALYSIS_LEASE_SECONDS.
"""

from __future__ import annotations

import base64
import json
import time
from typing import Any

import httpx
import pytest

import src.infrastructure.frame_sampler.client as client_module
from src.core.config import settings
from src.infrastructure.frame_sampler.client import (
    DEFAULT_TIMEOUT_SECONDS,
    FrameSamplerClient,
    FrameSamplerSettings,
    SamplerDisabled,
    SamplerUnavailable,
    build_frame_set,
    load_frame_sampler_settings,
)
from src.services.frame_sampling.budget import FramePlan
from src.services.frame_sampling.policy import FramePolicy
from src.services.frame_sampling.types import FrameSample, FrameSet
from src.services.system_config_registry import (
    FRAME_SAMPLER_ENABLED,
    FRAME_SAMPLER_TIMEOUT_SECONDS,
    FRAME_SAMPLER_URL,
    set_config,
)

URL = "http://frame-sampler:8000"

SAMPLE_BYTES = tuple(b"jpeg-payload-%03d" % i for i in range(3))
SAMPLE_B64 = [base64.b64encode(b).decode("ascii") for b in SAMPLE_BYTES]


class _Provider:
    """Tiny stand-in for an ``LLMProvider`` row (only ``extra_config_json``)."""

    def __init__(self, extra_config_json: Any = None) -> None:
        self.extra_config_json = extra_config_json


def _policy(**overrides: Any) -> FramePolicy:
    base: dict[str, Any] = {
        "mode": "dedupe",
        "target_context_ratio": 0.9,
        "max_edge": 640,
        "probe_fps": 2.0,
        "min_frames": 4,
        "max_frames": 120,
        "jpeg_quality": 85,
        "dedupe_phash_distance": 6,
        "window_pre_seconds": 5.0,
        "window_post_seconds": 10.0,
        "max_event_windows": 4,
        "max_output_tokens": 2048,
        "enabled": True,
        "mad_threshold": 1.0,
        "periodic_anchor_seconds": 8.0,
    }
    base.update(overrides)
    return FramePolicy(**base)


def _plan(frames: int = 12) -> FramePlan:
    duration = 60.0
    return FramePlan(
        frames=frames,
        timestamps=tuple(i * duration / frames for i in range(frames)),
        tokens_per_image=240,
        budget_tokens=frames * 240,
        clamp_reason="budget",
        duration_source="probe",
    )


def _settings(**overrides: Any) -> FrameSamplerSettings:
    base: dict[str, Any] = {
        "enabled": True,
        "url": URL,
        "timeout_seconds": 120.0,
    }
    base.update(overrides)
    return FrameSamplerSettings(**base)


def _ok_payload() -> dict[str, Any]:
    # ``candidates`` / ``duration_seconds`` are deliberately kept: the
    # sampler still sends them and the client must ignore them.
    return {
        "frames": [
            {"t_seconds": 0.0, "jpeg_base64": SAMPLE_B64[0]},
            {"t_seconds": 20.0, "jpeg_base64": SAMPLE_B64[1]},
            {"t_seconds": 40.0, "jpeg_base64": SAMPLE_B64[2]},
        ],
        "candidates": [{"t_start": 5.0, "t_end": 20.0, "reasons": ["scene"]}],
        "elapsed_ms": 123,
        "duration_seconds": 60.0,
    }


class _RecordingTransport:
    """Wraps a handler, recording every request (URL + JSON body)."""

    def __init__(self, handler: Any) -> None:
        self._handler = handler
        self.requests: list[dict[str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(
            {
                "url": str(request.url),
                "method": request.method,
                "body": json.loads(request.read().decode("utf-8")),
            }
        )
        return self._handler(request)

    @property
    def count(self) -> int:
        return len(self.requests)


def _client(
    handler: Any, settings: FrameSamplerSettings | None = None
) -> tuple[FrameSamplerClient, _RecordingTransport]:
    recorder = _RecordingTransport(handler)
    client = FrameSamplerClient(
        settings or _settings(),
        transport=httpx.MockTransport(recorder.handler),
    )
    return client, recorder


# ---------------------------------------------------------------------------
# Request body contract
# ---------------------------------------------------------------------------


def test_success_request_body_matches_contract_exactly() -> None:
    """Every key and value of the POST /sample body is pinned to the contract."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_ok_payload())

    client, recorder = _client(handler)
    result = client.sample(
        video_path="/abs/path.mp4",
        policy=_policy(),
        plan=_plan(frames=12),
        mode="dedupe",
    )

    assert recorder.count == 1
    request = recorder.requests[0]
    assert request["method"] == "POST"
    assert request["url"] == f"{URL}/sample"
    assert request["body"] == {
        "video_path": "/abs/path.mp4",
        "probe_fps": 2.0,
        "min_frames": 4,
        "max_frames": 12,
        "max_edge": 640,
        "jpeg_quality": 85,
        "mode": "dedupe",
        "dedupe_phash_distance": 6,
        "window_pre_seconds": 5.0,
        "window_post_seconds": 10.0,
        "max_event_windows": 4,
        "mad_threshold": 1.0,
        "periodic_anchor_seconds": 8.0,
    }
    assert result.elapsed_ms == 123


def test_success_body_omits_dedupe_distance_as_null() -> None:
    """``dedupe_phash_distance`` is serialized as JSON null when the policy has none."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_ok_payload())

    client, recorder = _client(handler)
    client.sample(
        video_path="/abs/path.mp4",
        policy=_policy(mode="event", dedupe_phash_distance=None),
        plan=_plan(frames=8),
        mode="event",
    )

    body = recorder.requests[0]["body"]
    assert body["dedupe_phash_distance"] is None
    assert body["mode"] == "event"
    assert body["max_frames"] == 8


def test_success_response_parsed_into_frameset() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_ok_payload())

    client, _ = _client(handler)
    result = client.sample(
        video_path="/abs/path.mp4",
        policy=_policy(),
        plan=_plan(),
        mode="dedupe",
    )

    assert result == FrameSet(
        frames=(
            FrameSample(t_seconds=0.0, jpeg_bytes=SAMPLE_BYTES[0]),
            FrameSample(t_seconds=20.0, jpeg_bytes=SAMPLE_BYTES[1]),
            FrameSample(t_seconds=40.0, jpeg_bytes=SAMPLE_BYTES[2]),
        ),
        source="sampler",
        elapsed_ms=123,
    )
    assert [frame.t_seconds for frame in result.frames] == [0.0, 20.0, 40.0]
    assert result.frames[0].jpeg_bytes == SAMPLE_BYTES[0]
    assert client.last_attempts == 1


def test_legacy_payload_keys_candidates_and_duration_are_ignored() -> None:
    """The sampler still sends ``candidates`` / ``duration_seconds``.

    The client must parse the payload fine and simply not surface those keys
    (the slim ``FrameSet`` has no field for them).
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_ok_payload())

    client, _ = _client(handler)
    result = client.sample(
        video_path="/abs/path.mp4",
        policy=_policy(),
        plan=_plan(),
        mode="dedupe",
    )

    assert len(result.frames) == 3
    assert result.elapsed_ms == 123
    assert not hasattr(result, "candidates")
    assert not hasattr(result, "duration_seconds")


# ---------------------------------------------------------------------------
# Mode contract
# ---------------------------------------------------------------------------


def test_keyframe_mode_is_accepted_in_request_body() -> None:
    """``keyframe`` is a valid contract mode and carries its two tunables."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_ok_payload())

    client, recorder = _client(handler)
    result = client.sample(
        video_path="/abs/path.mp4",
        policy=_policy(
            mode="keyframe",
            dedupe_phash_distance=6,
            mad_threshold=2.0,
            periodic_anchor_seconds=12.0,
        ),
        plan=_plan(frames=10),
        mode="keyframe",
    )

    assert recorder.count == 1
    body = recorder.requests[0]["body"]
    assert body["mode"] == "keyframe"
    assert body["mad_threshold"] == 2.0
    assert body["periodic_anchor_seconds"] == 12.0
    assert result.source == "sampler"


def test_two_new_tunables_sent_for_non_keyframe_modes() -> None:
    """``mad_threshold`` / ``periodic_anchor_seconds`` are sent for every mode."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_ok_payload())

    client, recorder = _client(handler)
    client.sample(
        video_path="/abs/path.mp4",
        policy=_policy(),
        plan=_plan(frames=8),
        mode="dedupe",
    )

    body = recorder.requests[0]["body"]
    assert body["mad_threshold"] == 1.0
    assert body["periodic_anchor_seconds"] == 8.0


@pytest.mark.parametrize("mode", ["bogus", "", "UNIFORM", "keyframes"])
def test_unknown_mode_raises_value_error_without_requests(mode: str) -> None:
    """An unknown mode is a programming error: ValueError, zero HTTP calls."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_ok_payload())

    client, recorder = _client(handler)
    with pytest.raises(ValueError, match="unknown sampling mode"):
        client.sample(video_path="/abs/path.mp4", policy=_policy(), plan=_plan(), mode=mode)

    assert recorder.count == 0


# ---------------------------------------------------------------------------
# Retry semantics
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_backoff_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip the deterministic backoff sleeps to keep the suite fast."""

    monkeypatch.setattr(time, "sleep", lambda seconds: None)


def test_503_then_200_succeeds_on_retry() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, json={"detail": "sampler busy"})
        return httpx.Response(200, json=_ok_payload())

    client, recorder = _client(handler)
    result = client.sample(
        video_path="/abs/path.mp4",
        policy=_policy(),
        plan=_plan(),
        mode="dedupe",
    )

    assert result.source == "sampler"
    assert len(result.frames) == 3
    assert recorder.count == 2
    assert client.last_attempts == 2


def test_three_503s_exhaust_retries() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "sampler busy"})

    client, recorder = _client(handler)
    with pytest.raises(SamplerUnavailable) as excinfo:
        client.sample(video_path="/abs/path.mp4", policy=_policy(), plan=_plan(), mode="dedupe")

    assert excinfo.value.last_status == 503
    assert excinfo.value.attempts == 3
    assert recorder.count == 3
    assert "503" in str(excinfo.value)


def test_connect_error_retried_then_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client, recorder = _client(handler)
    with pytest.raises(SamplerUnavailable) as excinfo:
        client.sample(video_path="/abs/path.mp4", policy=_policy(), plan=_plan(), mode="dedupe")

    assert excinfo.value.last_status is None
    assert excinfo.value.attempts == 3
    assert recorder.count == 3


def test_read_timeout_retried_then_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    client, recorder = _client(handler)
    with pytest.raises(SamplerUnavailable) as excinfo:
        client.sample(video_path="/abs/path.mp4", policy=_policy(), plan=_plan(), mode="dedupe")

    assert excinfo.value.last_status is None
    assert excinfo.value.attempts == 3
    assert recorder.count == 3


# ---------------------------------------------------------------------------
# 4xx: never retried
# ---------------------------------------------------------------------------


def test_400_surfaces_detail_without_retry() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"detail": "video_path is outside the allowed roots"})

    client, recorder = _client(handler)
    with pytest.raises(SamplerUnavailable) as excinfo:
        client.sample(video_path="/abs/path.mp4", policy=_policy(), plan=_plan(), mode="dedupe")

    assert "video_path is outside the allowed roots" in str(excinfo.value)
    assert excinfo.value.last_status == 400
    assert excinfo.value.attempts == 1
    assert recorder.count == 1


def test_422_undecodable_video_not_retried() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"detail": "video undecodable: no video stream"})

    client, recorder = _client(handler)
    with pytest.raises(SamplerUnavailable) as excinfo:
        client.sample(video_path="/abs/path.mp4", policy=_policy(), plan=_plan(), mode="dedupe")

    assert excinfo.value.last_status == 422
    assert recorder.count == 1


def test_400_long_detail_truncated_to_300_chars() -> None:
    # 300 leading chars + a distinguishable 7-char tail beyond the cut.
    long_detail = "x" * 300 + "TRAILER"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"detail": long_detail})

    client, recorder = _client(handler)
    with pytest.raises(SamplerUnavailable) as excinfo:
        client.sample(video_path="/abs/path.mp4", policy=_policy(), plan=_plan(), mode="dedupe")

    message = str(excinfo.value)
    assert long_detail[:300] in message
    assert "TRAILER" not in message
    assert recorder.count == 1


# ---------------------------------------------------------------------------
# Disabled / unconfigured fast-fail
# ---------------------------------------------------------------------------


def test_disabled_client_raises_without_requests() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_ok_payload())

    client, recorder = _client(handler, settings=_settings(enabled=False))
    with pytest.raises(SamplerDisabled) as excinfo:
        client.sample(video_path="/abs/path.mp4", policy=_policy(), plan=_plan(), mode="dedupe")

    assert "disabled" in str(excinfo.value).lower()
    assert recorder.count == 0


def test_blank_url_raises_without_requests() -> None:
    client, recorder = _client(
        lambda request: httpx.Response(200, json=_ok_payload()), settings=_settings(url="   ")
    )
    with pytest.raises(SamplerUnavailable) as excinfo:
        client.sample(video_path="/abs/path.mp4", policy=_policy(), plan=_plan(), mode="dedupe")

    assert str(excinfo.value) == "frame sampler URL is not configured"
    assert excinfo.value.last_status is None
    assert excinfo.value.attempts == 0
    assert recorder.count == 0


# ---------------------------------------------------------------------------
# Malformed 200 payloads
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("payload", "hint"),
    [
        ({"frames": "nope"}, "frames"),
        ({"elapsed_ms": 5}, "frames"),
        ({"frames": [{"t_seconds": 0.0}], "elapsed_ms": 5}, "jpeg_base64"),
        (
            {"frames": [{"t_seconds": 0.0, "jpeg_base64": "!!!not-base64"}], "elapsed_ms": 5},
            "base64",
        ),
        (
            {
                "frames": [
                    {"t_seconds": 10.0, "jpeg_base64": SAMPLE_B64[0]},
                    {"t_seconds": 5.0, "jpeg_base64": SAMPLE_B64[1]},
                ],
                "elapsed_ms": 5,
            },
            "ascending",
        ),
        (
            {"frames": [{"t_seconds": "soon", "jpeg_base64": SAMPLE_B64[0]}], "elapsed_ms": 5},
            "t_seconds",
        ),
        ({"frames": [], "elapsed_ms": "fast"}, "elapsed_ms"),
    ],
)
def test_malformed_200_body_raises(payload: dict[str, Any], hint: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    client, recorder = _client(handler)
    with pytest.raises(SamplerUnavailable) as excinfo:
        client.sample(video_path="/abs/path.mp4", policy=_policy(), plan=_plan(), mode="dedupe")

    assert hint in str(excinfo.value)
    assert recorder.count == 1


# ---------------------------------------------------------------------------
# build_frame_set port
# ---------------------------------------------------------------------------


def test_build_frame_set_video_mode_provider_raises_disabled() -> None:
    """A provider without ``frame_sampling`` stays in video mode: no sampler call."""

    with pytest.raises(SamplerDisabled) as excinfo:
        build_frame_set(_Provider(None), "/abs/path.mp4", _plan())

    assert str(excinfo.value) == "provider is in video input mode"


def test_build_frame_set_explicit_video_enabled_false_raises_disabled() -> None:
    provider = _Provider({"frame_sampling": {"enabled": False}})
    with pytest.raises(SamplerDisabled) as excinfo:
        build_frame_set(provider, "/abs/path.mp4", _plan())

    assert str(excinfo.value) == "provider is in video input mode"


class _FakeClient:
    """Records the call instead of touching the network."""

    instances: list[_FakeClient] = []

    def __init__(self, settings: FrameSamplerSettings, transport: Any = None) -> None:
        self.settings = settings
        self.call: dict[str, Any] | None = None
        _FakeClient.instances.append(self)

    def sample(
        self, *, video_path: str, policy: FramePolicy, plan: FramePlan, mode: str
    ) -> FrameSet:
        self.call = {"video_path": video_path, "policy": policy, "plan": plan, "mode": mode}
        return FrameSet(frames=(FrameSample(t_seconds=0.0, jpeg_bytes=b"ok"),), source="sampler")


@pytest.mark.parametrize(
    ("extra_config_json", "expected_mode"),
    [
        ({"frame_sampling": {}}, "event"),
        ({"frame_sampling": {"mode": "uniform"}}, "uniform"),
        ({"frame_sampling": {"mode": "dedupe"}}, "dedupe"),
        ({"frame_sampling": {"mode": "event"}}, "event"),
        ({"frame_sampling": {"mode": "keyframe"}}, "keyframe"),
        ({"frame_sampling": {"mode": "nope"}}, "event"),
        # legacy ``preset`` key: mapped to a mode by ``parse_frame_policy``
        ({"frame_sampling": {"preset": "baseline"}}, "uniform"),
        ({"frame_sampling": {"preset": "dedupe"}}, "dedupe"),
        ({"frame_sampling": {"preset": "event"}}, "event"),
        ({"frame_sampling": {"preset": "nope"}}, "event"),
    ],
)
def test_build_frame_set_delegates_to_client_with_policy_mode(
    pg_db: Any, monkeypatch: pytest.MonkeyPatch, extra_config_json: Any, expected_mode: str
) -> None:
    _FakeClient.instances = []
    monkeypatch.setattr(client_module, "FrameSamplerClient", _FakeClient)

    provider = _Provider(extra_config_json)
    plan = _plan(frames=12)
    result = build_frame_set(provider, "/abs/video.mp4", plan, db=pg_db)

    assert len(_FakeClient.instances) == 1
    fake = _FakeClient.instances[0]
    assert fake.call is not None
    assert fake.call["video_path"] == "/abs/video.mp4"
    assert fake.call["plan"] is plan
    assert fake.call["mode"] == expected_mode
    # settings come from the registry (defaults: disabled, blank URL, 120s)
    assert fake.settings.enabled is False
    assert fake.settings.url == ""
    assert fake.settings.timeout_seconds == 120.0
    assert result.source == "sampler"


def test_load_frame_sampler_settings_reads_registry(pg_db: Any) -> None:
    defaults = load_frame_sampler_settings(pg_db)
    assert defaults == FrameSamplerSettings(enabled=False, url="", timeout_seconds=120.0)

    set_config(pg_db, FRAME_SAMPLER_ENABLED, True)
    set_config(pg_db, FRAME_SAMPLER_URL, "http://sampler:8000/")
    set_config(pg_db, FRAME_SAMPLER_TIMEOUT_SECONDS, 90)
    loaded = load_frame_sampler_settings(pg_db)
    assert loaded == FrameSamplerSettings(
        enabled=True,
        url="http://sampler:8000/",
        timeout_seconds=90.0,
    )


# ---------------------------------------------------------------------------
# Timeout / lease invariant
# ---------------------------------------------------------------------------


def test_default_timeout_fits_analysis_lease() -> None:
    """The sampler call plus the LLM call must fit inside the analysis lease.

    The default 120s sampler timeout leaves at least 60s of headroom before
    ``ANALYSIS_LEASE_SECONDS`` (300s) is consumed by the follow-up LLM call.
    """

    assert DEFAULT_TIMEOUT_SECONDS == 120.0
    assert DEFAULT_TIMEOUT_SECONDS + 60 < settings.ANALYSIS_LEASE_SECONDS
