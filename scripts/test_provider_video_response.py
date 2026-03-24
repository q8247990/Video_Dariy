import argparse
import copy
import json

import httpx

from src.db.session import SessionLocal
from src.models.llm_provider import LLMProvider
from src.models.video_source import VideoSource
from src.models.video_session import VideoSession
from src.services.home_profile import build_home_context
from src.services.session_analysis_video import (
    build_chunk_video_data_url,
    build_session_video_chunks,
)
from src.tasks.analyzer import _build_prompt


def _provider_url(provider: LLMProvider) -> str:
    base = provider.api_base_url.rstrip("/")
    if not base.endswith("/v1") and "v1" not in base:
        base = f"{base}/v1"
    return f"{base}/chat/completions"


def _summarize_response(name: str, response: httpx.Response) -> None:
    print(f"=== {name} ===")
    print(f"status: {response.status_code}")
    print("body_excerpt:")
    print(response.text[:3000])

    try:
        data = response.json()
    except Exception as exc:
        print(f"json_error: {exc}")
        print()
        return

    choice = ((data.get("choices") or [{}])[0] or {}) if isinstance(data, dict) else {}
    message = choice.get("message") or {}
    content = message.get("content")
    reasoning = message.get("reasoning")
    summary = {
        "finish_reason": choice.get("finish_reason"),
        "content_type": type(content).__name__,
        "content_length": len(content) if isinstance(content, str) else None,
        "content_preview": content[:300] if isinstance(content, str) else repr(content),
        "reasoning_type": type(reasoning).__name__,
        "reasoning_length": len(reasoning) if isinstance(reasoning, str) else None,
        "reasoning_preview": reasoning[:300] if isinstance(reasoning, str) else repr(reasoning),
        "usage": data.get("usage") if isinstance(data, dict) else None,
    }
    print("parsed_summary:")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider-id", type=int, default=4)
    parser.add_argument("--session-id", type=int, default=51)
    args = parser.parse_args()

    db = SessionLocal()
    try:
        provider = db.query(LLMProvider).filter(LLMProvider.id == args.provider_id).first()
        if provider is None:
            raise ValueError(f"provider {args.provider_id} not found")

        session = db.query(VideoSession).filter(VideoSession.id == args.session_id).first()
        if session is None:
            raise ValueError(f"session {args.session_id} not found")

        source = db.query(VideoSource).filter(VideoSource.id == session.source_id).first()
        if source is None:
            raise ValueError(f"video source {session.source_id} not found")

        chunk = build_session_video_chunks(db, session.id, chunk_seconds=600)[0]
        video_data_url = build_chunk_video_data_url(chunk)
        prompt = _build_prompt(source, build_home_context(db), session, chunk)

        headers = {
            "Authorization": f"Bearer {provider.api_key}",
            "Content-Type": "application/json",
        }
        url = _provider_url(provider)

        base_video_payload = {
            "model": provider.model_name,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "video_url", "video_url": {"url": video_data_url}},
                        {"type": "text", "text": prompt},
                    ],
                }
            ],
            "temperature": 0,
            "max_tokens": 4096,
            "response_format": {"type": "json_object"},
        }

        variants = [
            ("baseline", {}),
            ("enable_thinking_false", {"enable_thinking": False}),
            ("thinking_false", {"thinking": False}),
            ("thinking_disabled_obj", {"thinking": {"type": "disabled"}}),
            (
                "chat_template_enable_thinking_false",
                {"chat_template_kwargs": {"enable_thinking": False}},
            ),
            (
                "reasoning_effort_none",
                {"reasoning_effort": "none"},
            ),
        ]

        with httpx.Client(timeout=180.0) as client:
            for name, extra_fields in variants:
                payload = copy.deepcopy(base_video_payload)
                payload.update(extra_fields)
                response = client.post(url, headers=headers, json=payload)
                _summarize_response(name, response)
    finally:
        db.close()


if __name__ == "__main__":
    main()
