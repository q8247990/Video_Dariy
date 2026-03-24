import os
import subprocess
from pathlib import Path

import pytest

from src.providers.openai_client import OpenAIClient
from src.services.session_analysis_video import SessionVideoChunk, build_chunk_video_data_url
from src.services.video_analysis.output_parser import parse_video_recognition_output


def _require_real_llm_env() -> tuple[str, str, str]:
    api_base_url = os.getenv("REAL_LLM_API_BASE_URL", "").strip()
    api_key = os.getenv("REAL_LLM_API_KEY", "").strip()
    model_name = os.getenv("REAL_LLM_MODEL_NAME", "").strip()
    if not api_base_url or not api_key or not model_name:
        pytest.skip(
            "缺少 REAL_LLM_API_BASE_URL / REAL_LLM_API_KEY / REAL_LLM_MODEL_NAME，"
            "跳过真实 LLM 集成测试"
        )
    return api_base_url, api_key, model_name


def _build_sample_video(path: Path, duration_seconds: int = 2) -> None:
    cmd = [
        "ffmpeg",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"color=c=black:s=640x360:d={duration_seconds}",
        "-f",
        "lavfi",
        "-i",
        f"anullsrc=r=44100:cl=stereo:d={duration_seconds}",
        "-shortest",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        str(path),
    ]
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        stderr = (result.stderr or b"")[-1000:].decode("utf-8", errors="ignore")
        raise RuntimeError(f"ffmpeg build sample video failed: {stderr}")


def _build_prompt() -> str:
    return (
        "你是家庭视频识别助手。请严格只输出 JSON 对象，不要输出 markdown。\n"
        "必须满足以下 schema：\n"
        "{\n"
        '  "session_summary": {\n'
        '    "summary_text": "string",\n'
        '    "activity_level": "low|medium|high",\n'
        '    "main_subjects": ["string"],\n'
        '    "has_important_event": true\n'
        "  },\n"
        '  "events": [\n'
        "    {\n"
        '      "offset_start_sec": 0,\n'
        '      "offset_end_sec": 1,\n'
        '      "event_type": "member_appear",\n'
        '      "title": "string",\n'
        '      "summary": "string",\n'
        '      "detail": "string",\n'
        '      "related_entities": [],\n'
        '      "observed_actions": [],\n'
        '      "interpreted_state": [],\n'
        '      "confidence": 0.9,\n'
        '      "importance_level": "low|medium|high"\n'
        "    }\n"
        "  ],\n"
        '  "analysis_notes": []\n'
        "}\n"
        "该视频是测试样本，通常没有关键事件。请尽量返回 events 为空数组。"
    )


@pytest.mark.integration
def test_real_llm_recognition_with_memory_chunk(tmp_path: Path) -> None:
    api_base_url, api_key, model_name = _require_real_llm_env()

    video_path = tmp_path / "sample_black.mp4"
    _build_sample_video(video_path)

    chunk = SessionVideoChunk(
        chunk_index=0,
        start_offset_seconds=0,
        duration_seconds=2,
        file_paths=[str(video_path)],
    )
    data_url = build_chunk_video_data_url(chunk)
    assert data_url.startswith("data:video/mp4;base64,")

    client = OpenAIClient(
        api_base_url=api_base_url,
        api_key=api_key,
        model_name=model_name,
        timeout=120,
    )

    response_text = client.chat_completion(
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "video_url",
                        "video_url": {"url": data_url},
                    },
                    {
                        "type": "text",
                        "text": _build_prompt(),
                    },
                ],
            }
        ],
        temperature=0,
        max_tokens=8192,
    )

    assert response_text
    parsed = parse_video_recognition_output(response_text)
    assert parsed.session_summary.summary_text
    assert parsed.session_summary.activity_level in {"low", "medium", "high"}
    assert isinstance(parsed.events, list)
    for event in parsed.events:
        assert event.offset_end_sec >= event.offset_start_sec
