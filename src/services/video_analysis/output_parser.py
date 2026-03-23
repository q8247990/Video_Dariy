import json

from pydantic import ValidationError

from src.services.video_analysis.schemas import RecognitionResultDTO


class RecognitionOutputError(ValueError):
    pass


class RecognitionOutputFormatError(RecognitionOutputError):
    pass


class RecognitionOutputValidationError(RecognitionOutputError):
    pass


def _strip_code_block(text: str) -> str:
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped

    lines = stripped.splitlines()
    if len(lines) < 3:
        return stripped
    return "\n".join(lines[1:-1]).strip()


def _extract_json_object(text: str) -> dict:
    decoder = json.JSONDecoder()
    for idx, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            result, _ = decoder.raw_decode(text[idx:])
        except json.JSONDecodeError:
            continue
        if isinstance(result, dict):
            return result

    raise RecognitionOutputFormatError("LLM response does not contain valid JSON object")


def _extract_all_json_objects(text: str) -> list[dict]:
    decoder = json.JSONDecoder()
    objects: list[dict] = []
    for idx, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            result, _ = decoder.raw_decode(text[idx:])
        except json.JSONDecodeError:
            continue
        if isinstance(result, dict):
            objects.append(result)
    return objects


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


def parse_video_recognition_output(raw_text: str) -> RecognitionResultDTO:
    text = _strip_code_block(raw_text)
    raw_candidates = _extract_all_json_objects(text)
    if not raw_candidates:
        raw_candidates = [_extract_json_object(text)]

    validation_errors: list[str] = []
    for raw_payload in raw_candidates:
        for payload in _normalize_recognition_payload(raw_payload):
            try:
                return RecognitionResultDTO.model_validate(payload)
            except ValidationError as e:
                validation_errors.append(str(e))
                continue

    merged_error = validation_errors[-1] if validation_errors else "unknown validation error"
    raise RecognitionOutputValidationError(f"Recognition output validation failed: {merged_error}")
