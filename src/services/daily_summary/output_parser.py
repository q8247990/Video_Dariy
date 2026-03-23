import json

from pydantic import ValidationError

from src.services.daily_summary.schemas import DailySummaryPromptResult


class DailySummaryOutputError(ValueError):
    pass


class DailySummaryOutputFormatError(DailySummaryOutputError):
    pass


class DailySummaryOutputValidationError(DailySummaryOutputError):
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
    start_idx = text.find("{")
    end_idx = text.rfind("}")
    if start_idx == -1 or end_idx == -1 or end_idx < start_idx:
        raise DailySummaryOutputFormatError("LLM response does not contain JSON object")

    payload = text[start_idx : end_idx + 1]
    try:
        result = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise DailySummaryOutputFormatError(f"Invalid JSON from summary provider: {exc}") from exc
    if not isinstance(result, dict):
        raise DailySummaryOutputFormatError("LLM JSON response must be an object")
    return result


def parse_daily_summary_output(raw_text: str) -> DailySummaryPromptResult:
    text = _strip_code_block(raw_text)
    payload = _extract_json_object(text)
    try:
        return DailySummaryPromptResult.model_validate(payload)
    except ValidationError as exc:
        raise DailySummaryOutputValidationError(
            f"Daily summary output validation failed: {exc}"
        ) from exc
