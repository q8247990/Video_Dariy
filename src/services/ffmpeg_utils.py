import os
import subprocess
import tempfile


def probe_video_duration_seconds(path: str, timeout: int = 30) -> float | None:
    """Return the real media duration (seconds) via ``ffprobe``.

    Used by the session-build dedupe stage to replace the historical
    hard-coded ``60``-second assumption with the file's real duration.
    Returns ``None`` (never raises) when ``ffprobe`` is missing, the
    file cannot be read, or the output cannot be parsed to a positive
    number — callers fall back to whatever duration they already had.
    """
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                path,
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        return None
    if result.returncode != 0:
        return None
    raw = (result.stdout or "").strip()
    if not raw or raw.upper() == "N/A":
        return None
    try:
        duration = float(raw)
    except ValueError:
        return None
    return duration if duration > 0 else None


def build_concat_file_text(paths: list[str]) -> str:
    lines = []
    for path in paths:
        escaped = path.replace("'", "'\\''")
        lines.append(f"file '{escaped}'")
    return "\n".join(lines) + "\n"


def run_ffmpeg_concat_to_file(
    source_paths: list[str],
    output_path: str,
    timeout: int = 300,
) -> None:
    concat_content = build_concat_file_text(source_paths)
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8"
    ) as tmp:
        tmp.write(concat_content)
        concat_file = tmp.name

    try:
        tmp_output = output_path + ".tmp.mp4"
        copy_cmd = [
            "ffmpeg",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            concat_file,
            "-c",
            "copy",
            tmp_output,
        ]
        copy_result = subprocess.run(copy_cmd, capture_output=True, text=True, timeout=timeout)
        if copy_result.returncode == 0:
            os.replace(tmp_output, output_path)
            return

        reencode_cmd = [
            "ffmpeg",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            concat_file,
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            tmp_output,
        ]
        reencode_result = subprocess.run(
            reencode_cmd, capture_output=True, text=True, timeout=timeout
        )
        if reencode_result.returncode != 0:
            copy_err = (copy_result.stderr or "")[-1000:]
            reencode_err = (reencode_result.stderr or "")[-1000:]
            raise ValueError(
                f"ffmpeg concat failed. copy_error={copy_err}; reencode_error={reencode_err}"
            )
        os.replace(tmp_output, output_path)
    finally:
        if os.path.exists(concat_file):
            os.unlink(concat_file)
