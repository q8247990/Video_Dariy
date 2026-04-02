import json
import logging
import re
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.orm import Session

from src.application.pipeline.commands import GenerateDailySummaryCommand, SendWebhookCommand
from src.application.pipeline.orchestrator import PipelineOrchestrator
from src.application.prompt.compiler import compile_daily_summary_prompt
from src.application.prompt.contracts import DailySummaryPromptInput
from src.core.celery_app import celery_app
from src.db.session import SessionLocal
from src.infrastructure.llm.openai_gateway import OpenAICompatGatewayFactory
from src.infrastructure.tasks.celery_dispatcher import CeleryTaskDispatcher
from src.models.daily_summary import DailySummary
from src.models.event_record import EventRecord
from src.models.system_config import SystemConfig
from src.models.task_log import TaskLog
from src.models.webhook_config import WebhookConfig
from src.services.app_runtime_state import get_runtime_state, set_runtime_state
from src.services.daily_summary.output_parser import (
    DailySummaryOutputError,
    parse_daily_summary_output,
)
from src.services.daily_summary.preprocess import (
    build_known_subjects,
    build_subject_event_mapping,
    extract_attention_candidates,
)
from src.services.daily_summary.schemas import AttentionItem
from src.services.home_profile import build_home_context
from src.services.llm_qos import enforce_token_quota, record_token_usage
from src.services.onboarding import DEFAULT_DAILY_SUMMARY_SCHEDULE
from src.services.pipeline_constants import TaskStatus, TaskType
from src.services.prompt_builder.v2.daily_summary import (
    build_daily_rollup_prompt,
    build_subject_summary_prompt,
    compress_daily_input,
)
from src.services.provider_selector import PROVIDER_TYPE_QA, find_required_enabled_provider
from src.services.task_dispatch_control import (
    TaskCancellationRequested,
    bind_or_create_running_task_log,
    ensure_task_not_cancelled,
    finalize_cancelled_task_log,
    finalize_task_log,
    get_task_log_for_update,
)
from src.services.webhook_payload import build_webhook_event_payload
from src.services.webhook_subscription import webhook_subscribes

logger = logging.getLogger(__name__)

WEBHOOK_EVENT_DAILY_SUMMARY_GENERATED = "daily_summary_generated"
DISPATCH_GUARD_STATE_KEY_PREFIX = "daily_summary_dispatch_guard"
SERIAL_SPLIT_PROMPT_THRESHOLD = 28000

_pipeline_orchestrator = PipelineOrchestrator(dispatcher=CeleryTaskDispatcher())


def _summary_title(target_date: date) -> str:
    return f"{target_date.strftime('%Y-%m-%d')} 家庭日报"


def _get_daily_schedule(db: Session) -> str:
    row = db.query(SystemConfig).filter(SystemConfig.config_key == "daily_summary_schedule").first()
    value = row.config_value if row else None
    if not value:
        return DEFAULT_DAILY_SUMMARY_SCHEDULE
    return str(value).strip() or DEFAULT_DAILY_SUMMARY_SCHEDULE


def _parse_schedule_time(value: str) -> tuple[int, int]:
    text = value.strip()
    parts = text.split(":")
    if len(parts) != 2:
        raise ValueError("schedule format must be HH:MM")

    hour = int(parts[0])
    minute = int(parts[1])
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        raise ValueError("schedule value out of range")
    return hour, minute


def _has_existing_summary_or_task(db: Session, target_date: date) -> bool:
    exists_summary = (
        db.query(DailySummary).filter(DailySummary.summary_date == target_date).first() is not None
    )
    if exists_summary:
        return True

    recent_logs = (
        db.query(TaskLog)
        .filter(TaskLog.task_type == "daily_summary_generation")
        .order_by(TaskLog.created_at.desc())
        .limit(20)
        .all()
    )
    date_str = str(target_date)
    for item in recent_logs:
        detail = item.detail_json if isinstance(item.detail_json, dict) else {}
        if detail.get("target_date") != date_str:
            continue
        if item.status in {"running", "success"}:
            return True
    return False


def _has_subscribed_webhook(db: Session, event_type: str) -> bool:
    hooks = db.query(WebhookConfig).filter(WebhookConfig.enabled.is_(True)).all()
    for hook in hooks:
        if webhook_subscribes(hook, event_type=event_type, version="1.0"):
            return True
    return False


def _upsert_daily_summary(
    db: Session,
    *,
    target_date: date,
    summary_title: str,
    overall_summary: str,
    structured_subject_sections: list[dict[str, Any]],
    structured_attention_items: list[dict[str, Any]],
    event_count: int,
    provider_id: int,
    provider_name_snapshot: str | None,
) -> DailySummary:
    payload = {
        "summary_date": target_date,
        "summary_title": summary_title,
        "overall_summary": overall_summary,
        "subject_sections_json": structured_subject_sections,
        "attention_items_json": structured_attention_items,
        "event_count": event_count,
        "provider_id": provider_id,
        "provider_name_snapshot": provider_name_snapshot,
        "generated_at": datetime.now(),
    }

    if db.bind is not None and db.bind.dialect.name == "postgresql":
        stmt = (
            postgresql_insert(DailySummary)
            .values(**payload)
            .on_conflict_do_update(
                index_elements=[DailySummary.summary_date],
                set_={
                    "summary_title": payload["summary_title"],
                    "overall_summary": payload["overall_summary"],
                    "subject_sections_json": payload["subject_sections_json"],
                    "attention_items_json": payload["attention_items_json"],
                    "event_count": payload["event_count"],
                    "provider_id": payload["provider_id"],
                    "provider_name_snapshot": payload["provider_name_snapshot"],
                    "generated_at": payload["generated_at"],
                },
            )
            .returning(DailySummary.id)
        )
        summary_id = db.execute(stmt).scalar_one()
        db.flush()
        return db.query(DailySummary).filter(DailySummary.id == summary_id).one()

    summary = db.query(DailySummary).filter(DailySummary.summary_date == target_date).first()
    if summary:
        summary.summary_title = summary_title
        summary.overall_summary = overall_summary
        summary.subject_sections_json = structured_subject_sections
        summary.attention_items_json = structured_attention_items
        summary.event_count = event_count
        summary.provider_id = provider_id
        summary.provider_name_snapshot = provider_name_snapshot
        summary.generated_at = payload["generated_at"]
    else:
        summary = DailySummary(**payload)
        db.add(summary)
    db.flush()
    return summary


def _build_fallback_overall_summary(events: list[EventRecord]) -> str:
    if not events:
        return "昨天家中整体较为平稳，未观测到明确的关键活动。"
    return "昨天家中有一定活动，系统已生成简要总结，建议查看事件明细以获取更多细节。"


def _truncate_text(value: str, max_len: int) -> str:
    text = value.strip()
    if len(text) <= max_len:
        return text
    return text[:max_len].rstrip("，。；,. ")


def _clean_generated_text(value: str) -> str:
    text = " ".join((value or "").strip().split())
    text = text.replace("…", "。")
    text = re.sub(r"\.{3,}", "。", text)
    text = re.sub(r"。{2,}", "。", text)
    text = re.sub(r"，{2,}", "，", text)
    return text.strip()


def _split_sentences(text: str) -> list[str]:
    cleaned = _clean_generated_text(text)
    if not cleaned:
        return []
    segments = re.split(r"(?<=[。！？!?])", cleaned)
    return [seg.strip() for seg in segments if seg.strip()]


def _trim_sentences_to_chars(text: str, max_chars: int) -> str:
    sentences = _split_sentences(text)
    if not sentences:
        return ""
    acc: list[str] = []
    total = 0
    for sent in sentences:
        if total + len(sent) > max_chars and acc:
            break
        if total + len(sent) > max_chars:
            return sent[:max_chars].strip(" ，。")
        acc.append(sent)
        total += len(sent)
    return "".join(acc).strip(" ，。")


def _estimate_detail_length(
    subject_sections: list[dict[str, Any]], attention_items: list[dict[str, Any]]
) -> int:
    total = 0
    for item in subject_sections:
        if not isinstance(item, dict):
            continue
        total += len(str(item.get("subject_name") or ""))
        total += len(str(item.get("summary") or ""))
        total += 8
    for item in attention_items:
        if not isinstance(item, dict):
            continue
        total += len(str(item.get("title") or ""))
        total += len(str(item.get("summary") or ""))
        total += 8
    return total


def _clamp_summary_payload(
    overall_summary: str,
    subject_sections: list[dict[str, Any]],
    attention_items: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    # 目标：overall + detail 控制在 500~900 字区间，尽量不切半句
    max_total = 900

    normalized_sections: list[dict[str, Any]] = []
    for item in subject_sections:
        if not isinstance(item, dict):
            continue
        current = dict(item)
        current["summary"] = _clean_generated_text(str(current.get("summary") or ""))
        normalized_sections.append(current)

    normalized_attention: list[dict[str, Any]] = []
    for item in attention_items[:3]:
        if not isinstance(item, dict):
            continue
        current = dict(item)
        current["summary"] = _clean_generated_text(str(current.get("summary") or ""))
        normalized_attention.append(current)

    normalized_overall = _clean_generated_text(overall_summary)

    detail_len = _estimate_detail_length(normalized_sections, normalized_attention)
    if len(normalized_overall) + detail_len <= max_total:
        return normalized_overall, normalized_sections, normalized_attention

    # 先收敛 overall 为完整句
    max_overall_len = max(120, max_total - detail_len)
    clamped_overall = _trim_sentences_to_chars(normalized_overall, max_overall_len)

    if len(clamped_overall) + detail_len <= max_total:
        return clamped_overall, normalized_sections, normalized_attention

    # 再收敛对象摘要为完整句
    for item in normalized_sections:
        item["summary"] = _trim_sentences_to_chars(str(item.get("summary") or ""), 80)

    detail_len = _estimate_detail_length(normalized_sections, normalized_attention)
    if len(clamped_overall) + detail_len <= max_total:
        return clamped_overall, normalized_sections, normalized_attention

    # 最后收敛关注事项
    for item in normalized_attention:
        item["summary"] = _trim_sentences_to_chars(str(item.get("summary") or ""), 60)

    return clamped_overall, normalized_sections, normalized_attention


def _parse_summary_with_retry(
    *,
    client: Any,
    prompt: str,
    initial_response_text: str | None,
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]], bool]:
    if initial_response_text:
        try:
            parsed = parse_daily_summary_output(initial_response_text)
            return (
                parsed.overall_summary,
                [item.model_dump() for item in parsed.subject_sections],
                [item.model_dump() for item in parsed.attention_items],
                False,
            )
        except DailySummaryOutputError as exc:
            logger.warning("Initial summary parse failed, retry with compact prompt: %s", exc)

    retry_prompt = "\n\n".join(
        [
            "请严格输出一个 JSON 对象，禁止输出解释文字、代码块或多余片段。",
            "如果字段缺失，请使用空数组或简短字符串占位，保持 JSON 可解析。",
            prompt,
        ]
    )
    retry_text = client.chat_completion(
        [
            {"role": "system", "content": "你负责生成结构化家庭日报。"},
            {"role": "user", "content": retry_prompt},
        ],
        temperature=0,
        max_tokens=8192,
    )
    parsed = parse_daily_summary_output(retry_text or "")
    return (
        parsed.overall_summary,
        [item.model_dump() for item in parsed.subject_sections],
        [item.model_dump() for item in parsed.attention_items],
        True,
    )


def _extract_json_payload(raw_text: str) -> dict[str, Any]:
    text = (raw_text or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if len(lines) >= 3:
            text = "\n".join(lines[1:-1]).strip()

    start_idx = text.find("{")
    end_idx = text.rfind("}")
    if start_idx < 0 or end_idx < start_idx:
        raise DailySummaryOutputError("LLM response does not contain JSON object")

    try:
        payload = json.loads(text[start_idx : end_idx + 1])
    except json.JSONDecodeError as exc:
        raise DailySummaryOutputError(f"Invalid JSON output: {exc}") from exc
    if not isinstance(payload, dict):
        raise DailySummaryOutputError("LLM JSON response must be an object")
    return payload


def _chat_completion_with_usage(
    *,
    db: Session,
    client: Any,
    provider_id: int,
    provider_name_snapshot: str | None,
    messages: list[dict[str, str]],
) -> str:
    text = client.chat_completion(
        messages,
        temperature=0,
        max_tokens=8192,
    )
    record_token_usage(
        db,
        provider_id=provider_id,
        provider_name_snapshot=provider_name_snapshot,
        scene="daily_summary",
        usage=client.get_last_usage(),
    )
    return text or ""


def _parse_subject_summary_output(raw_text: str, *, subject_name: str) -> tuple[str, bool]:
    payload = _extract_json_payload(raw_text)

    summary = str(payload.get("summary") or "").strip()
    attention_needed = bool(payload.get("attention_needed"))
    if summary:
        return summary, attention_needed

    if isinstance(payload.get("subject_sections"), list):
        parsed = parse_daily_summary_output(raw_text)
        matched = None
        for item in parsed.subject_sections:
            if item.subject_name == subject_name:
                matched = item
                break
        target = matched or (parsed.subject_sections[0] if parsed.subject_sections else None)
        if target is not None:
            return target.summary, bool(target.attention_needed)

    fallback_text = str(payload.get("overall_summary") or "").strip()
    if fallback_text:
        return _truncate_text(fallback_text, 120), False

    raise DailySummaryOutputError("subject summary output missing summary")


def _parse_rollup_output(raw_text: str) -> tuple[str, list[dict[str, Any]]]:
    payload = _extract_json_payload(raw_text)

    overall_summary = str(payload.get("overall_summary") or "").strip()
    attention_items = payload.get("attention_items")

    if not overall_summary and isinstance(payload.get("subject_sections"), list):
        parsed = parse_daily_summary_output(raw_text)
        return parsed.overall_summary, [item.model_dump() for item in parsed.attention_items]

    normalized_attention: list[dict[str, Any]] = []
    if isinstance(attention_items, list):
        for item in attention_items:
            if not isinstance(item, dict):
                continue
            try:
                normalized_attention.append(AttentionItem.model_validate(item).model_dump())
            except Exception:
                title = str(item.get("title") or "未命名关注项").strip()
                summary = str(item.get("summary") or "").strip()
                if not title or not summary:
                    continue
                level = str(item.get("level") or "medium").strip() or "medium"
                normalized_attention.append(
                    {
                        "title": title,
                        "summary": summary,
                        "level": level,
                    }
                )

    if not overall_summary:
        raise DailySummaryOutputError("rollup output missing overall_summary")

    return overall_summary, normalized_attention


def _build_subject_fallback_summary(subject_section: dict[str, Any]) -> str:
    subject_name = str(subject_section.get("subject_name") or "该对象")
    raw_count = int(subject_section.get("raw_event_count") or 0)
    raw_clusters = subject_section.get("clusters")
    clusters: list[dict[str, Any]] = raw_clusters if isinstance(raw_clusters, list) else []
    return f"{subject_name}当日有{raw_count}次相关活动，系统已提取{len(clusters)}类关键模式。"


def _complete_subject_sections(  # noqa: C901
    *,
    sections: list[dict[str, Any]],
    known_subjects: list[dict[str, str]],
    subject_sections_payload: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    activity_score_map: dict[str, int] = {}
    subject_type_map: dict[str, str] = {}

    for item in known_subjects:
        name = str(item.get("subject_name") or "").strip()
        if not name:
            continue
        subject_type_map[name] = str(item.get("subject_type") or "unknown")

    for item in subject_sections_payload:
        if not isinstance(item, dict):
            continue
        name = str(item.get("subject_name") or "").strip()
        if not name:
            continue
        activity_score_map[name] = int(item.get("related_event_count") or 0)
        subject_type_map[name] = str(
            item.get("subject_type") or subject_type_map.get(name) or "unknown"
        )

    normalized: list[dict[str, Any]] = []
    existing_names: set[str] = set()
    for item in sections:
        if not isinstance(item, dict):
            continue
        name = str(item.get("subject_name") or "").strip()
        if not name:
            continue
        existing_names.add(name)
        normalized.append(
            {
                "subject_name": name,
                "subject_type": str(
                    item.get("subject_type") or subject_type_map.get(name) or "unknown"
                ),
                "summary": _clean_generated_text(str(item.get("summary") or "")),
                "attention_needed": bool(item.get("attention_needed")),
                "activity_score": activity_score_map.get(name, 0),
            }
        )

    for subject_name, subject_type in subject_type_map.items():
        if subject_name in existing_names:
            continue
        score = activity_score_map.get(subject_name, 0)
        if score > 0:
            summary = f"{subject_name}当日有{score}次相关活动，整体状态平稳。"
        else:
            summary = f"{subject_name}当日未观察到明确活动。"
        normalized.append(
            {
                "subject_name": subject_name,
                "subject_type": subject_type,
                "summary": summary,
                "attention_needed": False,
                "activity_score": score,
            }
        )

    normalized.sort(key=lambda item: int(item.get("activity_score") or 0), reverse=True)
    return normalized


def _generate_single_pass_summary_payload(
    *,
    db: Session,
    client: Any,
    provider_id: int,
    provider_name_snapshot: str | None,
    events: list[EventRecord],
    prompt: tuple[str, str],
    known_subjects: list[dict[str, str]],
    subject_sections_payload: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]], int, bool]:
    system_prompt, user_prompt = prompt
    response_text = _chat_completion_with_usage(
        db=db,
        client=client,
        provider_id=provider_id,
        provider_name_snapshot=provider_name_snapshot,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    )

    overall_summary = _build_fallback_overall_summary(events)
    structured_subject_sections: list[dict[str, Any]] = []
    structured_attention_items: list[dict[str, Any]] = []
    parse_retried = False

    if response_text:
        try:
            parsed = parse_daily_summary_output(response_text)
            overall_summary = parsed.overall_summary
            structured_subject_sections = [item.model_dump() for item in parsed.subject_sections]
            structured_attention_items = [item.model_dump() for item in parsed.attention_items]
        except DailySummaryOutputError as exc:
            parse_retried = True
            logger.warning("Single-pass summary parse failed, retry once: %s", exc)
            retry_text = _chat_completion_with_usage(
                db=db,
                client=client,
                provider_id=provider_id,
                provider_name_snapshot=provider_name_snapshot,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": "请严格只输出JSON对象。\n\n" + user_prompt},
                ],
            )
            try:
                parsed = parse_daily_summary_output(retry_text)
                overall_summary = parsed.overall_summary
                structured_subject_sections = [
                    item.model_dump() for item in parsed.subject_sections
                ]
                structured_attention_items = [item.model_dump() for item in parsed.attention_items]
            except DailySummaryOutputError:
                overall_summary = _build_fallback_overall_summary(events)
                structured_subject_sections = []
                structured_attention_items = []

    completed_sections = _complete_subject_sections(
        sections=structured_subject_sections,
        known_subjects=known_subjects,
        subject_sections_payload=subject_sections_payload,
    )

    return (
        overall_summary,
        completed_sections,
        structured_attention_items,
        len(user_prompt),
        parse_retried,
    )


def _generate_serial_summary_payload(
    *,
    db: Session,
    client: Any,
    provider_id: int,
    provider_name_snapshot: str | None,
    target_date: date,
    start_dt: datetime,
    end_dt: datetime,
    events: list[EventRecord],
    home_context: dict[str, Any],
    known_subjects: list[dict[str, str]],
    subject_sections_payload: list[dict[str, Any]],
    missing_subjects: list[str],
    attention_candidates_payload: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]], int, bool]:
    compressed_input = compress_daily_input(
        subject_sections=subject_sections_payload,
        missing_subjects=missing_subjects,
        attention_candidates=attention_candidates_payload,
    )

    subject_type_by_name = {
        str(item.get("subject_name") or ""): str(item.get("subject_type") or "unknown")
        for item in known_subjects
        if str(item.get("subject_name") or "")
    }

    existing_names = {
        str(item.get("subject_name") or "") for item in compressed_input["subject_sections"]
    }
    for missing_name in missing_subjects:
        if missing_name in existing_names:
            continue
        compressed_input["subject_sections"].append(
            {
                "subject_name": missing_name,
                "subject_type": subject_type_by_name.get(missing_name, "unknown"),
                "raw_event_count": 0,
                "clusters": [],
            }
        )

    compressed_input["subject_sections"].sort(
        key=lambda item: int(item.get("raw_event_count") or 0),
        reverse=True,
    )

    overall_summary = _build_fallback_overall_summary(events)
    structured_subject_sections: list[dict[str, Any]] = []
    structured_attention_items: list[dict[str, Any]] = []
    prompt_chars_total = 0
    parse_retried = False

    for subject_section in compressed_input["subject_sections"]:
        subject_prompt = build_subject_summary_prompt(
            home_context=home_context,
            summary_date=target_date,
            time_range_start=start_dt.isoformat(),
            time_range_end=end_dt.isoformat(),
            subject_section=subject_section,
        )
        subject_system_prompt, subject_user_prompt = subject_prompt
        prompt_chars_total += len(subject_user_prompt)
        subject_name = str(subject_section.get("subject_name") or "未知对象")
        subject_type = str(subject_section.get("subject_type") or "unknown")

        subject_response_text = _chat_completion_with_usage(
            db=db,
            client=client,
            provider_id=provider_id,
            provider_name_snapshot=provider_name_snapshot,
            messages=[
                {"role": "system", "content": subject_system_prompt},
                {"role": "user", "content": subject_user_prompt},
            ],
        )

        try:
            subject_summary, attention_needed = _parse_subject_summary_output(
                subject_response_text,
                subject_name=subject_name,
            )
        except DailySummaryOutputError as exc:
            parse_retried = True
            logger.warning("Subject summary parse failed, retry once: %s", exc)
            retry_text = _chat_completion_with_usage(
                db=db,
                client=client,
                provider_id=provider_id,
                provider_name_snapshot=provider_name_snapshot,
                messages=[
                    {
                        "role": "system",
                        "content": subject_system_prompt,
                    },
                    {
                        "role": "user",
                        "content": "请严格只输出JSON对象。\n\n" + subject_user_prompt,
                    },
                ],
            )
            try:
                subject_summary, attention_needed = _parse_subject_summary_output(
                    retry_text,
                    subject_name=subject_name,
                )
            except DailySummaryOutputError:
                subject_summary = _build_subject_fallback_summary(subject_section)
                attention_needed = False

        structured_subject_sections.append(
            {
                "subject_name": subject_name,
                "subject_type": subject_type,
                "summary": _clean_generated_text(subject_summary),
                "attention_needed": attention_needed,
                "activity_score": int(subject_section.get("raw_event_count") or 0),
            }
        )

    structured_subject_sections.sort(
        key=lambda item: int(item.get("activity_score") or 0),
        reverse=True,
    )

    rollup_prompt = build_daily_rollup_prompt(
        home_context=home_context,
        summary_date=target_date,
        subject_results=structured_subject_sections,
        attention_candidates=compressed_input["attention_candidates"],
    )
    rollup_system_prompt, rollup_user_prompt = rollup_prompt
    prompt_chars_total += len(rollup_user_prompt)

    rollup_response_text = _chat_completion_with_usage(
        db=db,
        client=client,
        provider_id=provider_id,
        provider_name_snapshot=provider_name_snapshot,
        messages=[
            {"role": "system", "content": rollup_system_prompt},
            {"role": "user", "content": rollup_user_prompt},
        ],
    )
    try:
        overall_summary, structured_attention_items = _parse_rollup_output(rollup_response_text)
    except DailySummaryOutputError as exc:
        parse_retried = True
        logger.warning("Rollup summary parse failed, retry once: %s", exc)
        retry_text = _chat_completion_with_usage(
            db=db,
            client=client,
            provider_id=provider_id,
            provider_name_snapshot=provider_name_snapshot,
            messages=[
                {"role": "system", "content": rollup_system_prompt},
                {"role": "user", "content": "请严格只输出JSON对象。\n\n" + rollup_user_prompt},
            ],
        )
        try:
            overall_summary, structured_attention_items = _parse_rollup_output(retry_text)
        except DailySummaryOutputError:
            overall_summary = _build_fallback_overall_summary(events)
            structured_attention_items = []

    return (
        overall_summary,
        structured_subject_sections,
        structured_attention_items,
        prompt_chars_total,
        parse_retried,
    )


def _dispatch_guard_key(target_date: date) -> str:
    return f"{DISPATCH_GUARD_STATE_KEY_PREFIX}:{target_date.isoformat()}"


def _has_dispatch_guard(db: Session, target_date: date) -> bool:
    state_value = get_runtime_state(db, _dispatch_guard_key(target_date))
    return isinstance(state_value, dict) and bool(state_value.get("task_id"))


def _mark_dispatch_guard(db: Session, now: datetime, target_date: date, task_id: str) -> None:
    payload = {
        "target_date": str(target_date),
        "scheduled_for_date": now.date().isoformat(),
        "dispatched_at": now.isoformat(),
        "task_id": task_id,
    }
    set_runtime_state(db, _dispatch_guard_key(target_date), payload)


@celery_app.task(bind=True)
def dispatch_scheduled_daily_summary_task(self) -> dict:
    db: Session = SessionLocal()
    now = datetime.now()

    try:
        schedule_text = _get_daily_schedule(db)
        hour, minute = _parse_schedule_time(schedule_text)
    except ValueError as exc:
        logger.warning("Invalid daily summary schedule, skip dispatch: %s", exc)
        db.close()
        return {"scheduled": False, "reason": "invalid_schedule"}

    try:
        scheduled_at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if now < scheduled_at:
            return {"scheduled": False, "reason": "before_schedule"}

        target_date = (now - timedelta(days=1)).date()
        if _has_dispatch_guard(db, target_date):
            return {
                "scheduled": False,
                "reason": "dispatch_guard_blocked",
                "target_date": str(target_date),
            }

        if _has_existing_summary_or_task(db, target_date):
            return {"scheduled": False, "reason": "already_exists", "target_date": str(target_date)}

        task_id = _pipeline_orchestrator.dispatch_generate_daily_summary(
            GenerateDailySummaryCommand(target_date_str=str(target_date))
        )
        _mark_dispatch_guard(db, now, target_date, str(task_id))
        db.commit()
        return {"scheduled": True, "target_date": str(target_date), "task_id": task_id}
    except Exception as exc:
        db.rollback()
        logger.exception("Failed to dispatch scheduled daily summary for %s", now.date())
        return {
            "scheduled": False,
            "reason": "dispatch_failed",
            "error": str(exc),
            "target_date": str((now - timedelta(days=1)).date()),
        }
    finally:
        db.close()


@celery_app.task(bind=True)
def generate_daily_summary_task(self, target_date_str: str | None = None) -> dict:
    """
    Generate daily summary for a given date (YYYY-MM-DD).
    Defaults to yesterday.
    """
    db: Session = SessionLocal()

    if target_date_str:
        target_date = datetime.strptime(target_date_str, "%Y-%m-%d").date()
    else:
        target_date = datetime.now().date() - timedelta(days=1)

    queue_task_id = str(getattr(getattr(self, "request", None), "id", "") or "")
    task_log = bind_or_create_running_task_log(
        db,
        queue_task_id=queue_task_id or None,
        task_type=TaskType.DAILY_SUMMARY_GENERATION,
        task_target_id=None,
        detail_json={"target_date": str(target_date)},
    )
    db.commit()

    try:
        ensure_task_not_cancelled(
            db,
            task_log.id,
            default_message=f"Daily summary cancelled for {target_date}",
        )
        provider = find_required_enabled_provider(db, PROVIDER_TYPE_QA)
        enforce_token_quota(db, provider)

        # Get events for the target date
        start_dt = datetime.combine(target_date, datetime.min.time())
        end_dt = datetime.combine(target_date, datetime.max.time())

        events = (
            db.query(EventRecord)
            .filter(
                EventRecord.event_start_time >= start_dt, EventRecord.event_start_time <= end_dt
            )
            .order_by(EventRecord.event_start_time.asc())
            .all()
        )

        event_count = len(events)
        ensure_task_not_cancelled(
            db,
            task_log.id,
            default_message=f"Daily summary cancelled for {target_date}",
        )
        home_context = build_home_context(db)
        known_subjects = build_known_subjects(home_context)
        subject_sections, missing_subjects, mapped_event_ids = build_subject_event_mapping(
            events,
            known_subjects,
        )
        attention_candidates = extract_attention_candidates(events, mapped_event_ids)

        subject_sections_payload = [item.model_dump() for item in subject_sections]
        attention_candidates_payload = [item.model_dump() for item in attention_candidates]
        single_pass_prompt = compile_daily_summary_prompt(
            DailySummaryPromptInput(
                home_context=home_context,
                summary_date=target_date,
                time_range_start=start_dt.isoformat(),
                time_range_end=end_dt.isoformat(),
                subject_sections=subject_sections_payload,
                missing_subjects=missing_subjects,
                attention_candidates=attention_candidates_payload,
            )
        )

        client = OpenAICompatGatewayFactory().build(
            api_base_url=provider.api_base_url,
            api_key=provider.api_key,
            model_name=provider.model_name,
            timeout_seconds=provider.timeout_seconds,
        )

        summary_title = _summary_title(target_date)
        (
            overall_summary,
            structured_subject_sections,
            structured_attention_items,
            prompt_chars_total,
            parse_retried,
        ) = (
            _generate_serial_summary_payload(
                db=db,
                client=client,
                provider_id=provider.id,
                provider_name_snapshot=provider.provider_name,
                target_date=target_date,
                start_dt=start_dt,
                end_dt=end_dt,
                events=events,
                home_context=home_context,
                known_subjects=known_subjects,
                subject_sections_payload=subject_sections_payload,
                missing_subjects=missing_subjects,
                attention_candidates_payload=attention_candidates_payload,
            )
            if len(single_pass_prompt[1]) > SERIAL_SPLIT_PROMPT_THRESHOLD
            else _generate_single_pass_summary_payload(
                db=db,
                client=client,
                provider_id=provider.id,
                provider_name_snapshot=provider.provider_name,
                events=events,
                prompt=single_pass_prompt,
                known_subjects=known_subjects,
                subject_sections_payload=subject_sections_payload,
            )
        )

        summary_mode = (
            "split_serial"
            if len(single_pass_prompt[1]) > SERIAL_SPLIT_PROMPT_THRESHOLD
            else "single_pass"
        )

        ensure_task_not_cancelled(
            db,
            task_log.id,
            default_message=f"Daily summary cancelled for {target_date}",
        )

        overall_summary, structured_subject_sections, structured_attention_items = (
            _clamp_summary_payload(
                overall_summary,
                structured_subject_sections,
                structured_attention_items,
            )
        )

        _upsert_daily_summary(
            db,
            target_date=target_date,
            summary_title=summary_title,
            overall_summary=overall_summary,
            structured_subject_sections=structured_subject_sections,
            structured_attention_items=structured_attention_items,
            event_count=event_count,
            provider_id=provider.id,
            provider_name_snapshot=provider.provider_name,
        )

        ensure_task_not_cancelled(
            db,
            task_log.id,
            default_message=f"Daily summary cancelled for {target_date}",
        )

        if _has_subscribed_webhook(db, WEBHOOK_EVENT_DAILY_SUMMARY_GENERATED):
            payload = build_webhook_event_payload(
                WEBHOOK_EVENT_DAILY_SUMMARY_GENERATED,
                {
                    "date": str(target_date),
                    "summary_title": summary_title,
                    "overall_summary": overall_summary,
                    "subject_sections": structured_subject_sections,
                    "attention_items": structured_attention_items,
                    "event_count": event_count,
                },
                generated_at=datetime.now(),
            )
            try:
                _pipeline_orchestrator.dispatch_webhook(
                    SendWebhookCommand(
                        event_type=WEBHOOK_EVENT_DAILY_SUMMARY_GENERATED,
                        payload=payload,
                    )
                )
            except Exception:
                logger.exception("Failed to enqueue daily summary webhook task")

        detail: dict[str, Any] = {}
        raw_detail = task_log.detail_json
        if isinstance(raw_detail, dict):
            detail = {str(key): value for key, value in raw_detail.items()}
        detail.update(
            {
                "prompt_chars": prompt_chars_total,
                "single_pass_prompt_chars": len(single_pass_prompt[1]),
                "summary_mode": summary_mode,
                "split_threshold": SERIAL_SPLIT_PROMPT_THRESHOLD,
                "subject_sections_count": len(subject_sections),
                "attention_candidates_count": len(attention_candidates),
                "parse_retried": parse_retried,
            }
        )
        finalize_task_log(
            task_log,
            TaskStatus.SUCCESS,
            f"Generated summary for {target_date} with {event_count} events.",
            detail,
        )
        db.commit()
        return {"summary_date": str(target_date), "event_count": event_count}

    except TaskCancellationRequested as exc:
        logger.info("Daily summary task cancelled for %s", target_date)
        db.rollback()
        refreshed_task_log = get_task_log_for_update(db, task_log.id)
        if refreshed_task_log is None:
            raise
        finalize_cancelled_task_log(
            refreshed_task_log,
            str(exc),
            {"target_date": str(target_date), "cancelled": True},
        )
        db.commit()
        return {"cancelled": True, "summary_date": str(target_date)}

    except Exception as e:
        logger.exception("Failed to generate summary for %s", target_date)
        db.rollback()
        finalize_task_log(task_log, TaskStatus.FAILED, str(e))
        db.commit()
        raise e
    finally:
        db.close()
