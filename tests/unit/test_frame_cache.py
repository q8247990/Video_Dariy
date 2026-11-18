"""Unit tests for the on-disk frame-set cache and the acquire entry point (todo 21).

Covers:

- put/get round-trip: identical frames (bytes + timestamps + reasons) and
  elapsed_ms, ``source == "cache"``;
- cache-first semantics: a second ``acquire_frame_set`` of the same file
  does not re-invoke the sampler (call count stays at 1);
- invalidation: a changed policy field / changed ``plan.frames`` produces
  a different ``policy_key`` directory -> miss -> sampler called again;
- layout: ``<root>/frames/<path_hash>/<content_signature>/<policy_key>/``
  with the content signature as an explicit directory component;
- a truncated / corrupt ``frames.json`` -> clean miss, ``acquire``
  refetches and rewrites a readable manifest;
- a missing JPEG for a listed frame -> miss;
- video-mode provider -> ``SamplerDisabled`` (no sampler call);
- ``SamplerUnavailable`` propagates unchanged (no fallback, nothing cached);
- unusable cache root (exists as a file) -> loud OSError, never a silent
  success;
- atomic writes leave no ``*.tmp`` leftovers.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import src.infrastructure.frame_sampler.acquire as acquire_module
from src.core.config import settings
from src.infrastructure.frame_sampler.acquire import acquire_frame_set
from src.infrastructure.frame_sampler.client import SamplerDisabled, SamplerUnavailable
from src.services.frame_sampling.budget import FramePlan
from src.services.frame_sampling.cache import FrameSetCache
from src.services.frame_sampling.fingerprint import content_signature
from src.services.frame_sampling.policy import FramePolicy, parse_frame_policy
from src.services.frame_sampling.types import FrameSample, FrameSet

FRAME_BYTES: tuple[bytes, ...] = tuple(b"jpeg-payload-%03d" % i for i in range(3))


class _Provider:
    """Tiny stand-in for an ``LLMProvider`` row (only ``extra_config_json``)."""

    def __init__(self, extra_config_json: Any = None) -> None:
        self.extra_config_json = extra_config_json


def _image_provider(max_edge: int = 640) -> _Provider:
    return _Provider({"frame_sampling": {"mode": "dedupe", "max_edge": max_edge}})


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


def _frame_set(elapsed_ms: int = 123) -> FrameSet:
    return FrameSet(
        frames=(
            FrameSample(t_seconds=0.0, jpeg_bytes=FRAME_BYTES[0], reason="uniform"),
            FrameSample(t_seconds=20.0, jpeg_bytes=FRAME_BYTES[1], reason="uniform"),
            FrameSample(t_seconds=40.0, jpeg_bytes=FRAME_BYTES[2], reason="scene"),
        ),
        source="sampler",
        elapsed_ms=elapsed_ms,
    )


class _FakeBuildFrameSet:
    """Records calls; returns the canned frame set (no network, no DB)."""

    def __init__(self) -> None:
        self.calls = 0
        self.error: Exception | None = None

    def __call__(
        self, provider: Any, video_path: str, plan: FramePlan, *, db: Any = None
    ) -> FrameSet:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return _frame_set()


@pytest.fixture
def video_file(tmp_path: Path) -> Path:
    video = tmp_path / "cam1" / "2026-01-01_120000.mp4"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"\x00\x01\x02" * 20_000)
    return video


@pytest.fixture
def cache_dir(tmp_path: Path) -> Path:
    """Fresh (not yet created) cache root for direct FrameSetCache tests."""
    return tmp_path / "frame-cache"


@pytest.fixture
def cache(cache_dir: Path) -> FrameSetCache:
    return FrameSetCache(cache_dir)


@pytest.fixture
def acquire_cache_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Patch ``settings.PLAYBACK_CACHE_ROOT`` to an isolated directory."""
    root = tmp_path / "hls-cache"
    root.mkdir()
    monkeypatch.setattr(settings, "PLAYBACK_CACHE_ROOT", str(root))
    return root


@pytest.fixture
def fake_sampler(acquire_cache_root: Path, monkeypatch: pytest.MonkeyPatch) -> _FakeBuildFrameSet:
    fake = _FakeBuildFrameSet()
    monkeypatch.setattr(acquire_module, "build_frame_set", fake)
    return fake


def _entry_dir(root: Path, video_path: Path, policy: FramePolicy, plan: FramePlan) -> Path:
    return (
        root
        / "frames"
        / FrameSetCache.path_hash(str(video_path))
        / content_signature(video_path)
        / FrameSetCache.policy_key(policy, plan)
    )


# ---------------------------------------------------------------------------
# FrameSetCache: put/get round-trip
# ---------------------------------------------------------------------------


def test_put_then_get_returns_identical_frameset_from_cache(
    cache: FrameSetCache, video_file: Path
) -> None:
    policy = parse_frame_policy(_image_provider())
    plan = _plan()
    original = _frame_set()

    cache.put(video_path=str(video_file), policy=policy, plan=plan, frame_set=original)
    got = cache.get(video_path=str(video_file), policy=policy, plan=plan)

    assert got is not None
    assert got.source == "cache"
    assert got.elapsed_ms == original.elapsed_ms
    assert got.frames == original.frames
    assert [f.t_seconds for f in got.frames] == [0.0, 20.0, 40.0]
    assert [f.jpeg_bytes for f in got.frames] == list(FRAME_BYTES)
    assert [f.reason for f in got.frames] == ["uniform", "uniform", "scene"]


def test_old_manifest_with_legacy_keys_is_tolerated(
    cache: FrameSetCache, cache_dir: Path, video_file: Path
) -> None:
    """A manifest written before the slim contract still reads as a hit.

    The old layout carried ``candidates`` and ``duration_seconds`` keys; the
    reader must ignore them (frames + elapsed_ms are what the contract keeps).
    """
    policy = parse_frame_policy(_image_provider())
    plan = _plan()
    entry_dir = _entry_dir(cache_dir, video_file, policy, plan)
    entry_dir.mkdir(parents=True)
    (entry_dir / "frame-000001.jpg").write_bytes(FRAME_BYTES[0])
    legacy_manifest = {
        "version": 1,
        "source": "sampler",
        "elapsed_ms": 7,
        "duration_seconds": 60.0,
        "frames": [{"t_seconds": 0.0, "reason": "uniform", "file": "frame-000001.jpg"}],
        "candidates": [{"t_start": 5.0, "t_end": 20.0, "reasons": ["scene", "motion"]}],
    }
    (entry_dir / "frames.json").write_text(
        json.dumps(legacy_manifest, sort_keys=True), encoding="utf-8"
    )

    got = cache.get(video_path=str(video_file), policy=policy, plan=plan)

    assert got is not None
    assert got.source == "cache"
    assert got.elapsed_ms == 7
    assert [f.jpeg_bytes for f in got.frames] == [FRAME_BYTES[0]]


def test_get_on_fresh_root_is_a_clean_miss(cache: FrameSetCache, video_file: Path) -> None:
    assert (
        cache.get(
            video_path=str(video_file),
            policy=parse_frame_policy(_image_provider()),
            plan=_plan(),
        )
        is None
    )


def test_purge_removes_all_entries_for_a_video(
    cache: FrameSetCache, cache_dir: Path, video_file: Path
) -> None:
    plan = _plan()
    policy_a = parse_frame_policy(_image_provider(max_edge=640))
    policy_b = parse_frame_policy(_image_provider(max_edge=960))
    cache.put(video_path=str(video_file), policy=policy_a, plan=plan, frame_set=_frame_set())
    cache.put(video_path=str(video_file), policy=policy_b, plan=plan, frame_set=_frame_set())
    assert list((cache_dir / "frames").iterdir())

    cache.purge(str(video_file))

    assert not (cache_dir / "frames" / FrameSetCache.path_hash(str(video_file))).exists()


# ---------------------------------------------------------------------------
# Layout & invalidation keys
# ---------------------------------------------------------------------------


def test_cache_layout_uses_path_hash_and_content_signature_dirs(
    cache: FrameSetCache, cache_dir: Path, video_file: Path
) -> None:
    policy = parse_frame_policy(_image_provider())
    plan = _plan()
    cache.put(video_path=str(video_file), policy=policy, plan=plan, frame_set=_frame_set())

    path_hash_dir = cache_dir / "frames" / FrameSetCache.path_hash(str(video_file))
    assert path_hash_dir.is_dir()
    # the content signature is an explicit directory component
    assert [p.name for p in path_hash_dir.iterdir()] == [content_signature(video_file)]
    signature_dir = path_hash_dir / content_signature(video_file)
    # and under it exactly one policy_key directory
    policy_key = FrameSetCache.policy_key(policy, plan)
    assert [p.name for p in signature_dir.iterdir()] == [policy_key]
    entry = signature_dir / policy_key
    assert (entry / "frames.json").is_file()
    assert (entry / "frame-000001.jpg").is_file()
    assert (entry / "frame-000002.jpg").is_file()
    assert (entry / "frame-000003.jpg").is_file()
    manifest = json.loads((entry / "frames.json").read_text(encoding="utf-8"))
    assert manifest["version"] == 1
    assert manifest["source"] == "sampler"
    assert manifest["elapsed_ms"] == 123
    assert "duration_seconds" not in manifest
    assert "candidates" not in manifest
    assert [f["file"] for f in manifest["frames"]] == [
        "frame-000001.jpg",
        "frame-000002.jpg",
        "frame-000003.jpg",
    ]


def test_policy_key_changes_with_policy_field_and_plan_frames() -> None:
    plan = _plan(frames=12)
    policy_a = parse_frame_policy(_image_provider(max_edge=640))
    policy_b = parse_frame_policy(_image_provider(max_edge=960))
    key_a = FrameSetCache.policy_key(policy_a, plan)
    key_b = FrameSetCache.policy_key(policy_b, plan)
    key_plan = FrameSetCache.policy_key(policy_a, _plan(frames=8))
    assert key_a != key_b
    assert key_a != key_plan
    assert len(key_a) == 64  # sha256 hex


# ---------------------------------------------------------------------------
# Miss semantics: corrupt manifest, missing JPEG, atomicity
# ---------------------------------------------------------------------------


def test_truncated_frames_json_is_a_clean_miss(
    cache: FrameSetCache, cache_dir: Path, video_file: Path
) -> None:
    policy = parse_frame_policy(_image_provider())
    plan = _plan()
    cache.put(video_path=str(video_file), policy=policy, plan=plan, frame_set=_frame_set())
    manifest = _entry_dir(cache_dir, video_file, policy, plan) / "frames.json"
    manifest.write_bytes(b'{"version": 1, "frames": [{"t_seconds": 0.0, "rea')

    assert cache.get(video_path=str(video_file), policy=policy, plan=plan) is None
    # the stale entry is left in place (a put overwrites it; no eager delete)
    assert manifest.exists()


def test_missing_jpeg_is_a_clean_miss(
    cache: FrameSetCache, cache_dir: Path, video_file: Path
) -> None:
    policy = parse_frame_policy(_image_provider())
    plan = _plan()
    cache.put(video_path=str(video_file), policy=policy, plan=plan, frame_set=_frame_set())
    (_entry_dir(cache_dir, video_file, policy, plan) / "frame-000002.jpg").unlink()

    assert cache.get(video_path=str(video_file), policy=policy, plan=plan) is None


def test_put_leaves_no_tmp_leftovers(
    cache: FrameSetCache, cache_dir: Path, video_file: Path
) -> None:
    cache.put(
        video_path=str(video_file),
        policy=parse_frame_policy(_image_provider()),
        plan=_plan(),
        frame_set=_frame_set(),
    )
    assert list(cache_dir.glob("**/*.tmp")) == []


def test_cache_root_is_a_file_raises_oserror(tmp_path: Path, video_file: Path) -> None:
    blocking = tmp_path / "blocking"
    blocking.write_text("not a directory")
    cache = FrameSetCache(blocking)
    policy = parse_frame_policy(_image_provider())
    plan = _plan()

    with pytest.raises(OSError):
        cache.get(video_path=str(video_file), policy=policy, plan=plan)
    with pytest.raises(OSError):
        cache.put(video_path=str(video_file), policy=policy, plan=plan, frame_set=_frame_set())


# ---------------------------------------------------------------------------
# acquire_frame_set: cache-first entry point
# ---------------------------------------------------------------------------


def test_second_acquire_hits_cache_and_skips_sampler(
    acquire_cache_root: Path, fake_sampler: _FakeBuildFrameSet, video_file: Path
) -> None:
    provider = _image_provider()
    plan = _plan()

    first = acquire_frame_set(provider, str(video_file), plan)
    second = acquire_frame_set(provider, str(video_file), plan)

    assert fake_sampler.calls == 1
    assert first.source == "sampler"
    assert second.source == "cache"
    assert second.frames == first.frames
    assert second.elapsed_ms == first.elapsed_ms


def test_acquire_layout_has_content_signature_dir_component(
    acquire_cache_root: Path, fake_sampler: _FakeBuildFrameSet, video_file: Path
) -> None:
    provider = _image_provider()
    plan = _plan()
    acquire_frame_set(provider, str(video_file), plan)

    entry = _entry_dir(
        acquire_cache_root,
        video_file,
        parse_frame_policy(provider),
        plan,
    )
    assert (entry / "frames.json").is_file()
    assert (entry / "frame-000001.jpg").is_file()
    # recompute the two structural components independently
    signature_dir = (
        acquire_cache_root
        / "frames"
        / FrameSetCache.path_hash(str(video_file))
        / content_signature(video_file)
    )
    assert signature_dir.is_dir()


def test_changed_policy_field_is_a_cache_miss(
    acquire_cache_root: Path, fake_sampler: _FakeBuildFrameSet, video_file: Path
) -> None:
    plan = _plan()
    first = acquire_frame_set(_image_provider(max_edge=640), str(video_file), plan)
    second = acquire_frame_set(_image_provider(max_edge=960), str(video_file), plan)

    assert fake_sampler.calls == 2
    assert first.source == "sampler"
    assert second.source == "sampler"
    signature_dir = (
        acquire_cache_root
        / "frames"
        / FrameSetCache.path_hash(str(video_file))
        / content_signature(video_file)
    )
    keys = sorted(p.name for p in signature_dir.iterdir())
    key_a = FrameSetCache.policy_key(parse_frame_policy(_image_provider(max_edge=640)), plan)
    key_b = FrameSetCache.policy_key(parse_frame_policy(_image_provider(max_edge=960)), plan)
    assert key_a != key_b
    assert keys == sorted([key_a, key_b])


def test_changed_plan_frames_is_a_cache_miss(
    acquire_cache_root: Path, fake_sampler: _FakeBuildFrameSet, video_file: Path
) -> None:
    provider = _image_provider()
    first = acquire_frame_set(provider, str(video_file), _plan(frames=12))
    second = acquire_frame_set(provider, str(video_file), _plan(frames=8))

    assert fake_sampler.calls == 2
    assert first.source == "sampler"
    assert second.source == "sampler"
    signature_dir = (
        acquire_cache_root
        / "frames"
        / FrameSetCache.path_hash(str(video_file))
        / content_signature(video_file)
    )
    keys = sorted(p.name for p in signature_dir.iterdir())
    key_12 = FrameSetCache.policy_key(parse_frame_policy(provider), _plan(frames=12))
    key_8 = FrameSetCache.policy_key(parse_frame_policy(provider), _plan(frames=8))
    assert keys == sorted([key_12, key_8])


def test_truncated_frames_json_refetches_and_rewrites(
    acquire_cache_root: Path, fake_sampler: _FakeBuildFrameSet, video_file: Path
) -> None:
    provider = _image_provider()
    plan = _plan()
    policy = parse_frame_policy(provider)
    acquire_frame_set(provider, str(video_file), plan)
    fake_sampler.calls = 0

    manifest = _entry_dir(acquire_cache_root, video_file, policy, plan) / "frames.json"
    manifest.write_bytes(b"\x00\x01garbage-not-json")
    cache = FrameSetCache(acquire_cache_root)
    assert cache.get(video_path=str(video_file), policy=policy, plan=plan) is None

    again = acquire_frame_set(provider, str(video_file), plan)

    assert fake_sampler.calls == 1
    assert again.source == "sampler"
    # the manifest was rewritten and is readable again
    reloaded = json.loads(manifest.read_text(encoding="utf-8"))
    assert reloaded["version"] == 1
    assert reloaded["source"] == "sampler"
    got = cache.get(video_path=str(video_file), policy=policy, plan=plan)
    assert got is not None
    assert got.frames == again.frames


@pytest.mark.parametrize(
    "extra_config_json",
    [None, {"frame_sampling": {"enabled": False}}, {"other_key": 1}],
)
def test_video_mode_provider_raises_sampler_disabled(
    acquire_cache_root: Path,
    fake_sampler: _FakeBuildFrameSet,
    video_file: Path,
    extra_config_json: Any,
) -> None:
    with pytest.raises(SamplerDisabled):
        acquire_frame_set(_Provider(extra_config_json), str(video_file), _plan())
    assert fake_sampler.calls == 0


def test_sampler_unavailable_propagates_and_caches_nothing(
    acquire_cache_root: Path, fake_sampler: _FakeBuildFrameSet, video_file: Path
) -> None:
    fake_sampler.error = SamplerUnavailable("sampler down", last_status=503, attempts=3)

    with pytest.raises(SamplerUnavailable):
        acquire_frame_set(_image_provider(), str(video_file), _plan())

    assert fake_sampler.calls == 1
    assert not (acquire_cache_root / "frames").exists()


def test_cache_root_is_a_file_acquire_raises_and_never_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_sampler: _FakeBuildFrameSet,
    video_file: Path,
) -> None:
    blocking = tmp_path / "blocking"
    blocking.write_text("not a directory")
    monkeypatch.setattr(settings, "PLAYBACK_CACHE_ROOT", str(blocking))

    with pytest.raises(NotADirectoryError) as excinfo:
        acquire_frame_set(_image_provider(), str(video_file), _plan())

    assert isinstance(excinfo.value, OSError)
    assert "not a directory" in str(excinfo.value).lower()
    assert fake_sampler.calls == 0
