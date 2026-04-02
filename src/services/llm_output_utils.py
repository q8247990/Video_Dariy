import json


def strip_code_block(text: str) -> str:
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if len(lines) < 3:
        return stripped
    return "\n".join(lines[1:-1]).strip()


def extract_json_object(text: str, *, strict: bool = False) -> dict:
    if strict:
        decoder = json.JSONDecoder()
        stripped = text.lstrip()
        if not stripped.startswith("{"):
            raise ValueError("LLM response does not start with a JSON object")
        try:
            result, end_index = decoder.raw_decode(stripped)
        except json.JSONDecodeError as exc:
            raise ValueError(
                "LLM response does not contain a complete top-level JSON object"
            ) from exc
        if not isinstance(result, dict):
            raise ValueError("LLM response top-level JSON must be an object")
        trailing = stripped[end_index:].strip()
        if trailing:
            raise ValueError("LLM response contains unexpected trailing content")
        return result
    else:
        start_idx = text.find("{")
        end_idx = text.rfind("}")
        if start_idx == -1 or end_idx == -1 or end_idx < start_idx:
            raise ValueError("LLM response does not contain JSON object")
        payload = text[start_idx : end_idx + 1]
        try:
            result = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON from LLM provider: {exc}") from exc
        if not isinstance(result, dict):
            raise ValueError("LLM JSON response must be an object")
        return result


def truncate_text(value: str | None, max_length: int) -> str | None:
    if value is None:
        return None
    text = value.strip()
    if len(text) <= max_length:
        return text
    return f"{text[: max_length - 3]}..."
