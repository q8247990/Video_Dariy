import os
import subprocess
import tempfile


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


def run_ffmpeg_concat_to_bytes(
    source_paths: list[str],
    timeout: int = 300,
) -> bytes:
    concat_content = build_concat_file_text(source_paths).encode("utf-8")
    with tempfile.NamedTemporaryFile(mode="wb", suffix=".txt", delete=False) as tmp:
        tmp.write(concat_content)
        concat_file = tmp.name

    try:
        copy_cmd = [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            concat_file,
            "-c",
            "copy",
            "-movflags",
            "frag_keyframe+empty_moov",
            "-f",
            "mp4",
            "pipe:1",
        ]
        copy_result = subprocess.run(copy_cmd, capture_output=True, timeout=timeout)
        if copy_result.returncode == 0 and copy_result.stdout:
            return copy_result.stdout

        reencode_cmd = [
            "ffmpeg",
            "-v",
            "error",
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
            "frag_keyframe+empty_moov",
            "-f",
            "mp4",
            "pipe:1",
        ]
        reencode_result = subprocess.run(reencode_cmd, capture_output=True, timeout=timeout)
        if reencode_result.returncode != 0 or not reencode_result.stdout:
            copy_err = (copy_result.stderr or b"")[-1000:].decode("utf-8", errors="ignore")
            reencode_err = (reencode_result.stderr or b"")[-1000:].decode("utf-8", errors="ignore")
            raise ValueError(
                "ffmpeg concat to bytes failed. "
                f"copy_error={copy_err}; reencode_error={reencode_err}"
            )
        return reencode_result.stdout
    finally:
        if os.path.exists(concat_file):
            os.unlink(concat_file)
