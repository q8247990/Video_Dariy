"""Sub-chunk execution stage.

Performs the one piece of work that *cannot* hold a checked-out DB
connection: the ffmpeg-decode (when multiple files are concatenated)
followed by the vLLM HTTP request. Everything in this module is
deliberately DB-free; the caller passes a fully-loaded
:class:`LLMGatewayPort` and receives back a :class:`SubChunkRunResult`
that the checkpoint writer then persists.

The only "side effect" of this stage is the read of file bytes from
disk (and, for multi-file sub-chunks, an ffmpeg concatenation in
memory). It does not import :mod:`src.db.session` and does not take a
SQLAlchemy session argument — that's the structural enforcement of
the "no long-lived DB connection during LLM call" requirement.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from src.application.ports.llm_gateway import LLMGatewayPort
from src.models.llm_provider import LLMProvider
from src.services.analysis.chunk_plan import SubChunkPlan
from src.services.analysis.constants import RAW_MP4_NUM_FRAMES
from src.services.session_analysis_video import (
    build_chunk_video_data_url,
    session_chunk_from_sub_chunk,
)
from src.services.video_analysis.schemas import RecognitionResultDTO


@dataclass(frozen=True)
class SubChunkRunResult:
    """Return value of :func:`execute_sub_chunk`.

    Carries everything the checkpoint writer needs to persist a
    success row: the parsed DTO, the token usage extracted from the
    last gateway call, and the raw response text (used as the
    ``last_raw_response_text`` snapshot in failure logs).
    """

    recognition_result: RecognitionResultDTO
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    usage: dict[str, int]
    raw_response_text: Optional[str]
    video_data_url: str
    extra_body: dict[str, Any]


def _build_messages(
    *,
    system_prompt: str,
    user_prompt: str,
    video_data_url: str,
) -> list[dict[str, Any]]:
    user_content: list[dict[str, Any]] = [
        {"type": "video_url", "video_url": {"url": video_data_url}},
        {"type": "text", "text": user_prompt},
    ]
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]


def _token_counts_from_usage(
    usage: dict[str, int] | None,
) -> tuple[int, int, int]:
    prompt_tokens = int((usage or {}).get("prompt_tokens") or 0)
    completion_tokens = int((usage or {}).get("completion_tokens") or 0)
    total_tokens = int((usage or {}).get("total_tokens") or 0)
    if total_tokens <= 0:
        total_tokens = prompt_tokens + completion_tokens
    return prompt_tokens, completion_tokens, total_tokens


def build_sub_chunk_video_url(
    *,
    plan_chunk_index: int,
    sub_chunk: SubChunkPlan,
) -> str:
    """Read / concatenate the sub-chunk's MP4 bytes and base64-wrap them.

    Pure function — no LLM, no DB. Exposed separately so unit tests
    can pin the ``data:video/mp4;base64,...`` prefix and the
    ``RAW_MP4_NUM_FRAMES`` ``media_io_kwargs`` constant without
    touching the gateway.
    """
    projected = session_chunk_from_sub_chunk(
        sub_chunk.to_sub_chunk(), parent_chunk_index=plan_chunk_index
    )
    return build_chunk_video_data_url(projected)


def build_sub_chunk_extra_body() -> dict[str, Any]:
    """Return the ``media_io_kwargs.video.num_frames`` hint."""
    return {"media_io_kwargs": {"video": {"num_frames": RAW_MP4_NUM_FRAMES}}}


def execute_sub_chunk(
    *,
    client: LLMGatewayPort,
    provider: LLMProvider,
    sub_chunk: SubChunkPlan,
    plan_chunk_index: int,
    system_prompt: str,
    user_prompt: str,
    video_data_url: str | None = None,
    extra_body: dict[str, Any] | None = None,
    response_parser: Any = None,
) -> SubChunkRunResult:
    """Build the raw_mp4 payload, call the gateway, parse the JSON.

    Holds the DB connection only indirectly: callers must have
    already committed/rolled back before invoking this function. The
    function itself does not import SQLAlchemy and the type checker
    will catch any regression that introduces one.

    The raw LLM response text is captured *before* the response
    parser runs, so a parser exception still surfaces the raw text
    to the failure log — the pre-Wave-5 contract the unit tests
    depend on.

    ``video_data_url`` / ``extra_body`` / ``response_parser`` accept
    pre-computed values from the caller. When ``video_data_url`` is
    None the helper builds it via
    :func:`build_sub_chunk_video_url`; when ``response_parser`` is
    None the helper uses
    :func:`src.services.video_analysis.output_parser.parse_video_recognition_output`.
    The caller is expected to pass values resolved through its own
    monkey-patchable names so the existing unit-test fixtures keep
    working unchanged.
    """
    del provider
    if video_data_url is None:
        video_data_url = build_sub_chunk_video_url(
            plan_chunk_index=plan_chunk_index, sub_chunk=sub_chunk
        )
    if extra_body is None:
        extra_body = build_sub_chunk_extra_body()
    messages = _build_messages(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        video_data_url=video_data_url,
    )

    response_text = client.chat_completion(
        messages=messages,
        temperature=0,
        max_tokens=8192,
        response_format={"type": "json_object"},
        extra_body=extra_body,
    )
    raw_response_text = client.get_last_raw_response_text()
    if not response_text:
        raise ValueError(
            "Empty response from vision provider for sub-chunk "
            f"{sub_chunk.chunk_index}-{sub_chunk.sub_chunk_index}"
        )

    if response_parser is None:
        from src.services.video_analysis.output_parser import (
            parse_video_recognition_output,
        )

        recognition_result = parse_video_recognition_output(response_text)
    else:
        recognition_result = response_parser(response_text)
    usage = client.get_last_usage() or {}
    prompt_tokens, completion_tokens, total_tokens = _token_counts_from_usage(usage)

    return SubChunkRunResult(
        recognition_result=recognition_result,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        usage=dict(usage),
        raw_response_text=raw_response_text,
        video_data_url=video_data_url,
        extra_body=extra_body,
    )


__all__ = [
    "SubChunkRunResult",
    "build_sub_chunk_extra_body",
    "build_sub_chunk_video_url",
    "execute_sub_chunk",
]
