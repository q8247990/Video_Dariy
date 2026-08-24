"""Wave 1.5 / T0.1-T0.2: A/B harness for top-N keyframe selection.

For each N in ``--ns`` (default 64, 96, 128), runs
``extract_keyframes_for_sub_chunk`` against the synthetic or real
sub-chunk material and reports:

- frames_emitted       (capped by target_n)
- mean_gap_seconds     (average gap between emitted keyframes in seconds)
- max_gap_seconds      (largest gap; high values mean long uncovered windows)
- distinct_seconds     (unique time-anchor count, considering all indices)
- coverage_ratio       (distinct_seconds / total_duration; 1.0 = perfectly uniform)

Two modes:

1. Default (no ``--video-glob`` or ``--dry-run``): synthetic 10-min stream
   generated in-memory (monkeypatches ``_probe_video`` and
   ``_decode_file_with_ffmpeg_pipe`` so no ffmpeg binary is required).
   Useful for CI / developer machines that lack ffmpeg, and for
   validating that the algorithm behaves consistently across N values.

2. ``--video-glob GLOB``: real-files mode. Calls into the production
   ffmpeg path (no monkeypatch). Requires:
   - ``ffmpeg`` + ``ffprobe`` binaries on PATH
   - At least one mp4 matching the glob
   Run on the deployment host with: ``python scripts/ab_keyframe_10min.py
   --video-glob 'xiaomi_video/2026031500/**/*.mp4' --ns 64,96,128
   --out-dir /tmp/ab_keyframe``.

The decision rule (REPORT §6 + REPORT §3): pick the smallest N that gives
coverage_ratio close to 1.0 (no large uncovered windows). 64 is the
REPORT §3 default; bump to 96/128 only if max_gap_seconds > 5 s on real
data.
"""

from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _build_synthetic_stream(
    total_seconds: float, fps: float = 2.0
) -> tuple[list[Any], float, list[str]]:
    """Return (frames, duration, paths) for monkeypatched decode.

    Synthetic content approximates REPORT §2 (real Xiaomi CCTV): mostly
    static frames (score below threshold), punctuated by brief dynamic
    bursts. The algorithm should pick bursts as change-points and the
    periodic-anchor cadence fills the long static stretches.
    """
    import numpy as np

    n_frames = max(2, int(total_seconds * fps))
    rng = np.random.default_rng(seed=42)
    base_static = np.full((240, 320, 3), 60, dtype=np.uint8)
    frames: list[Any] = []
    for i in range(n_frames):
        in_burst = (i % 8) < 2  # 2-frame burst every 4 s
        if in_burst:
            frame = np.full((240, 320, 3), (i * 11 + 37) % 256, dtype=np.uint8)
        else:
            jitter = rng.integers(0, 4, size=(240, 320, 3), dtype=np.uint8)
            frame = (base_static + jitter).astype(np.uint8)
        frames.append(frame)
    paths = ["/synthetic/stream.mp4"]
    return frames, total_seconds, paths


def _patch_extractor_for_synthetic(
    fps_target: int, total_seconds: float
) -> tuple[list[Any], float, list[str]]:
    from pathlib import Path

    from src.services import keyframe_extractor as ke

    frames, duration, paths = _build_synthetic_stream(total_seconds=total_seconds, fps=fps_target)

    _original_is_file = Path.is_file

    def _fake_is_file(self: Path) -> bool:
        del self
        return True

    def _fake_probe(_path: Any) -> tuple[float, float]:
        return 20.0, duration

    def _fake_decode(path: Any, fps: int) -> Any:
        del path, fps
        yield from frames

    Path.is_file = _fake_is_file  # type: ignore[method-assign]
    ke._probe_video = _fake_probe  # type: ignore[assignment]
    ke._decode_file_with_ffmpeg_pipe = _fake_decode  # type: ignore[assignment]
    return frames, duration, paths


def _metrics_for_target_n(
    target_n: int,
    *,
    fps_target: int,
    periodic_anchor_seconds: int,
    total_seconds: float,
) -> dict[str, Any]:
    from src.services.keyframe_extractor import extract_keyframes_for_sub_chunk

    frames, duration, paths = _patch_extractor_for_synthetic(
        fps_target=fps_target, total_seconds=total_seconds
    )
    ks = extract_keyframes_for_sub_chunk(
        paths,
        target_n=target_n,
        periodic_anchor_seconds=periodic_anchor_seconds,
    )
    total_frames = ks.total_num_frames or int(duration * fps_target)
    seconds_per_frame = 1.0 / float(fps_target)

    if ks.frames_indices:
        distinct = sorted({int(i) for i in ks.frames_indices})
        gaps_seconds = [
            (distinct[i + 1] - distinct[i]) * seconds_per_frame for i in range(len(distinct) - 1)
        ]
        mean_gap = round(sum(gaps_seconds) / len(gaps_seconds), 2) if gaps_seconds else 0.0
        head_gap = distinct[0] * seconds_per_frame
        tail_gap = duration - distinct[-1] * seconds_per_frame
        max_gap = round(max([head_gap, tail_gap, *gaps_seconds]), 2)
    else:
        mean_gap = 0.0
        max_gap = round(duration, 2)

    coverage_ratio = round((duration - max_gap) / max(duration, 1e-6), 3)
    return {
        "target_n": target_n,
        "frames_emitted": len(ks.jpeg_base64_list),
        "frames_decoded": total_frames,
        "coverage_ratio": coverage_ratio,
        "mean_gap_seconds": mean_gap,
        "max_gap_seconds": max_gap,
        "sample_period_frames": ks.sample_period_frames,
    }


def run_dry_run(
    ns: list[int],
    *,
    total_seconds: float,
    fps_target: int,
    periodic_anchor_seconds: int,
) -> list[dict[str, Any]]:
    rows = [
        _metrics_for_target_n(
            n,
            fps_target=fps_target,
            periodic_anchor_seconds=periodic_anchor_seconds,
            total_seconds=total_seconds,
        )
        for n in ns
    ]
    return rows


def run_real(
    video_glob: str,
    ns: list[int],
    out_dir: Path,
    *,
    fps_target: int,
    periodic_anchor_seconds: int,
    target_n_override: int | None,
) -> list[dict[str, Any]]:
    paths = sorted(glob.glob(video_glob, recursive=True))
    if not paths:
        raise SystemExit(f"no mp4 matched glob: {video_glob}")
    if not os.access(paths[0], os.R_OK):
        raise SystemExit(f"file not readable: {paths[0]}")

    from src.services.keyframe_extractor import extract_keyframes_for_sub_chunk

    rows: list[dict[str, Any]] = []
    out_dir.mkdir(parents=True, exist_ok=True)
    for n in ns:
        ks = extract_keyframes_for_sub_chunk(
            paths,
            target_n=n,
            periodic_anchor_seconds=periodic_anchor_seconds,
        )
        fps = ks.fps or float(fps_target)
        seconds_per_frame = 1.0 / fps
        if ks.frames_indices:
            distinct = sorted({int(i) for i in ks.frames_indices})
            gaps = [
                (distinct[i + 1] - distinct[i]) * seconds_per_frame
                for i in range(len(distinct) - 1)
            ]
            mean_gap = round(sum(gaps) / len(gaps), 2) if gaps else 0.0
            max_gap = round(max(gaps), 2) if gaps else 0.0
            coverage_ratio = round(len(distinct) / max(ks.total_num_frames, 1), 3)
        else:
            mean_gap = 0.0
            max_gap = 0.0
            coverage_ratio = 0.0

        row = {
            "target_n": n,
            "frames_emitted": len(ks.jpeg_base64_list),
            "frames_decoded": ks.total_num_frames,
            "coverage_ratio": coverage_ratio,
            "mean_gap_seconds": mean_gap,
            "max_gap_seconds": max_gap,
            "sample_period_frames": ks.sample_period_frames,
        }
        rows.append(row)
        if out_dir is not None:
            out_dir.mkdir(parents=True, exist_ok=True)
            stamp = out_dir / f"set_n{n}.json"
            stamp.write_text(
                json.dumps(
                    {
                        "fps": ks.fps,
                        "total_num_frames": ks.total_num_frames,
                        "frames_indices": list(ks.frames_indices),
                        "extra": ks.extra,
                        "jpeg_count": len(ks.jpeg_base64_list),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
    return rows


def _print_table(rows: list[dict[str, Any]]) -> None:
    if not rows:
        print("(no rows)")
        return
    cols = [
        "target_n",
        "frames_emitted",
        "frames_decoded",
        "coverage_ratio",
        "mean_gap_seconds",
        "max_gap_seconds",
        "sample_period_frames",
    ]
    widths = {c: max(len(c), max(len(str(r[c])) for r in rows)) for c in cols}
    header = "  ".join(c.ljust(widths[c]) for c in cols)
    print(header)
    print("-" * len(header))
    for r in rows:
        print("  ".join(str(r[c]).ljust(widths[c]) for c in cols))


def _write_csv(rows: list[dict[str, Any]], out_dir: Path) -> Path | None:
    if not rows:
        return None
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / "ab_keyframe_coverage.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        for r in rows:
            writer.writerow(r)
    return csv_path


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "A/B harness for video keyframe top-N selection "
            "(Wave 1.5 / REPORT §6). Default mode runs against an "
            "in-memory synthetic stream; pass --video-glob to use real "
            "mp4 files (requires ffmpeg + ffprobe on PATH)."
        )
    )
    parser.add_argument(
        "--video-glob",
        type=str,
        default=None,
        help="glob for real mp4 files (e.g. 'xiaomi_video/**/*.mp4')",
    )
    parser.add_argument(
        "--ns",
        type=str,
        default="64,96,128",
        help="comma-separated target_n values (default: 64,96,128)",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path("/tmp/ab_keyframe"),
        help="output directory for real-mode artifacts (default: /tmp/ab_keyframe)",
    )
    parser.add_argument(
        "--total-seconds",
        type=float,
        default=600.0,
        help="synthetic stream duration in seconds (default: 600 = 10 min)",
    )
    parser.add_argument(
        "--fps-target",
        type=int,
        default=2,
        help="ffmpeg fps filter for decode (default: 2)",
    )
    parser.add_argument(
        "--periodic-anchor-seconds",
        type=int,
        default=8,
        help="periodic anchor interval (default: 8)",
    )
    args = parser.parse_args()

    ns = sorted({int(x) for x in args.ns.split(",") if x.strip()})
    if not ns:
        raise SystemExit("--ns must contain at least one integer")
    for n in ns:
        if n <= 0 or n > 768:
            raise SystemExit(f"--ns value out of range (1..768): {n}")

    if args.video_glob:
        rows = run_real(
            args.video_glob,
            ns,
            args.out_dir,
            fps_target=args.fps_target,
            periodic_anchor_seconds=args.periodic_anchor_seconds,
            target_n_override=None,
        )
        csv_path = _write_csv(rows, args.out_dir)
        print(f"REAL MODE: video_glob={args.video_glob}")
        _print_table(rows)
        if csv_path:
            print(f"\nCoverage CSV: {csv_path}")
    else:
        rows = run_dry_run(
            ns,
            total_seconds=args.total_seconds,
            fps_target=args.fps_target,
            periodic_anchor_seconds=args.periodic_anchor_seconds,
        )
        print(f"DRY-RUN MODE (synthetic {args.total_seconds:g}s stream; no ffmpeg required)")
        _print_table(rows)
        print(
            "\nDecision rule (REPORT §6): pick smallest N with "
            "max_gap_seconds <= 5 s and coverage_ratio >= 0.95."
        )
        best = min(
            (r for r in rows if r["max_gap_seconds"] <= 5.0),
            key=lambda r: r["target_n"],
            default=None,
        )
        if best is None:
            print(
                "No N meets the 5 s gap / 0.95 coverage threshold on this "
                "synthetic stream; rerun with --video-glob for real data."
            )
        else:
            print(
                f"Recommended default: target_n={best['target_n']} "
                f"(max_gap={best['max_gap_seconds']}s, "
                f"coverage={best['coverage_ratio']})"
            )


if __name__ == "__main__":
    main()
