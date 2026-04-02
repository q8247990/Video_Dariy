from pydantic import ValidationError

from src.services.llm_output_utils import extract_json_object, strip_code_block
from src.services.video_analysis.enums import (
    normalize_activity_level,
    normalize_analysis_note_type,
    normalize_event_type,
)
from src.services.video_analysis.schemas import RecognitionResultDTO


class RecognitionOutputError(ValueError):
    pass


class RecognitionOutputFormatError(RecognitionOutputError):
    pass


class RecognitionOutputValidationError(RecognitionOutputError):
    pass


def _looks_like_session_summary(payload: dict) -> bool:
    return all(
        key in payload
        for key in ["summary_text", "activity_level", "main_subjects", "has_important_event"]
    )


def _normalize_recognition_payload(payload: dict) -> list[dict]:
    candidates: list[dict] = [payload]

    nested_data = payload.get("data")
    if isinstance(nested_data, dict):
        candidates.append(nested_data)

    if _looks_like_session_summary(payload) and (
        "events" in payload or "analysis_notes" in payload
    ):
        candidates.append(
            {
                "session_summary": payload,
                "events": payload.get("events", []),
                "analysis_notes": payload.get("analysis_notes", []),
            }
        )

    if "session_summary" not in payload and ("events" in payload or "analysis_notes" in payload):
        summary_from_root = {
            "summary_text": payload.get("summary_text", "自动生成摘要"),
            "activity_level": payload.get("activity_level", "medium"),
            "main_subjects": payload.get("main_subjects", []),
            "has_important_event": payload.get("has_important_event", False),
        }
        if summary_from_root["summary_text"]:
            candidates.append(
                {
                    "session_summary": summary_from_root,
                    "events": payload.get("events", []),
                    "analysis_notes": payload.get("analysis_notes", []),
                }
            )

    return [item for item in candidates if isinstance(item, dict)]


def _sanitize_recognition_payload(payload: dict) -> dict:
    sanitized = dict(payload)

    events = sanitized.get("events")
    if not isinstance(events, list):
        events = []
    sanitized_events: list[dict] = []
    for item in events:
        if not isinstance(item, dict):
            continue
        normalized = dict(item)
        normalized["event_type"] = normalize_event_type(item.get("event_type"))
        sanitized_events.append(normalized)
    sanitized["events"] = sanitized_events

    summary = sanitized.get("session_summary")
    if isinstance(summary, dict):
        normalized_summary = dict(summary)
        normalized_summary["activity_level"] = normalize_activity_level(
            summary.get("activity_level"),
            has_events=bool(sanitized_events),
        )
        sanitized["session_summary"] = normalized_summary

    notes = sanitized.get("analysis_notes")
    if not isinstance(notes, list):
        notes = []
    sanitized_notes: list[dict] = []
    for item in notes:
        if not isinstance(item, dict):
            continue
        normalized = dict(item)
        normalized["type"] = normalize_analysis_note_type(item.get("type"))
        sanitized_notes.append(normalized)
    sanitized["analysis_notes"] = sanitized_notes

    return sanitized


def parse_video_recognition_output(raw_text: str) -> RecognitionResultDTO:
    text = strip_code_block(raw_text)
    try:
        raw_payload = extract_json_object(text, strict=True)
    except ValueError as exc:
        raise RecognitionOutputFormatError(str(exc)) from exc

    validation_errors: list[str] = []
    for payload in _normalize_recognition_payload(raw_payload):
        try:
            return RecognitionResultDTO.model_validate(_sanitize_recognition_payload(payload))
        except ValidationError as e:
            validation_errors.append(str(e))
            continue

    merged_error = validation_errors[0] if validation_errors else "unknown validation error"
    raise RecognitionOutputValidationError(f"Recognition output validation failed: {merged_error}")
