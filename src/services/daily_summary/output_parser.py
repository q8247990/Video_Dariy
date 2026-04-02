from pydantic import ValidationError

from src.services.daily_summary.schemas import DailySummaryPromptResult
from src.services.llm_output_utils import extract_json_object, strip_code_block


class DailySummaryOutputError(ValueError):
    pass


class DailySummaryOutputFormatError(DailySummaryOutputError):
    pass


class DailySummaryOutputValidationError(DailySummaryOutputError):
    pass


def parse_daily_summary_output(raw_text: str) -> DailySummaryPromptResult:
    text = strip_code_block(raw_text)
    try:
        payload = extract_json_object(text)
    except ValueError as exc:
        raise DailySummaryOutputFormatError(str(exc)) from exc
    try:
        return DailySummaryPromptResult.model_validate(payload)
    except ValidationError as exc:
        raise DailySummaryOutputValidationError(
            f"Daily summary output validation failed: {exc}"
        ) from exc
