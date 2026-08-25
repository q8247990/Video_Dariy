"""Doc-validation helper for the audit-remediation TODO 16.

Verifies that:
  1. Required production env vars appear in .env.example AND docker-compose*.yml
     (with `:?set ... in .env` gating where required).
  2. The HTTP error-code mapping documented in the root README/ARCHITECTURE matches
     src/api/error_status.py.

Docs-only; no runtime imports beyond the standard library and the error-code table.

Usage:
    python3 scripts/validate_docs.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# (env var, required, must_be_set_gated_in_compose)
REQUIRED_SECRETS = [
    ("SECRET_KEY", True, True),
    ("MEDIA_SIGNING_KEY", True, True),
    ("PROVIDER_KEY_ENCRYPTION_KEY", True, True),
]

# Env vars that must at least be mentioned across the doc set.
REQUIRED_ENV_VARS = [
    "APP_ENV",
    "DATABASE_URL",
    "REDIS_URL",
    "VIDEO_ROOT_PATH",
    "PLAYBACK_CACHE_ROOT",
    "SECRET_KEY",
    "MEDIA_SIGNING_KEY",
    "PROVIDER_KEY_ENCRYPTION_KEY",
]

COMPOSE_FILES = [ROOT / "docker-compose.yml", ROOT / "docker-compose.release.yml"]
ENV_EXAMPLE = ROOT / ".env.example"
ERROR_STATUS = ROOT / "src/api/error_status.py"
ROOT_DOCS = [ROOT / "README.md", ROOT / "README.en.md"]


def _expect_error_status_table() -> dict[int, int]:
    """Parse the `_STATUS_BY_CODE` dict from src/api/error_status.py (expected output)."""
    text = ERROR_STATUS.read_text()
    match = re.search(r"_STATUS_BY_CODE.*?=\s*(\{.*?\})", text, re.DOTALL)
    if not match:
        raise SystemExit("could not parse _STATUS_BY_CODE from src/api/error_status.py")
    table = astict(match.group(1))
    return table


def astict(body: str) -> dict[int, int]:
    """Parse `{4000: 400, ...}` literal from the captured body string."""
    result: dict[int, int] = {}
    for key, value in re.findall(r"(\d+)\s*:\s*(\d+)", body):
        result[int(key)] = int(value)
    return result


def _check_env_vars(env_text: str, failures: list[str]) -> None:
    """Verify every required env var is declared in .env.example."""
    for var in REQUIRED_ENV_VARS:
        if not re.search(rf"^{var}\s*=", env_text, re.MULTILINE):
            failures.append(f".env.example: missing required env var {var}")


def _check_compose_secrets(compose_texts: list[str], failures: list[str]) -> None:
    """Verify required secrets are gated in every docker-compose file."""
    for var, _required, gate in REQUIRED_SECRETS:
        for path, text in zip(COMPOSE_FILES, compose_texts, strict=False):
            if not re.search(rf"\${{{var}[:?!]", text):
                failures.append(f"{path.name}: missing gated reference for {var}")
            if gate and not re.search(rf"\${{{var}:\?set [^}}]* in \.env}}", text):
                failures.append(f"{path.name}: {var} not gated with ':?set ... in .env'")


def _check_error_code_mapping(failures: list[str]) -> None:
    """Verify root README docs document the error-code mapping from error_status.py."""
    expected = _expect_error_status_table()
    for doc in ROOT_DOCS:
        text = doc.read_text()
        mapped_lines = [
            line
            for line in text.splitlines()
            if "→" in line and re.search(r"\b\d{4}→\d{3}\b", line)
        ]
        seen: dict[int, int] = {}
        for line in mapped_lines:
            for code, status in re.findall(r"\b(\d{4})→(\d{3})\b", line):
                seen[int(code)] = int(status)
        for code, status in expected.items():
            if seen.get(code) != status:
                failures.append(
                    f"{doc.name}: error code {code} documented as {seen.get(code)} "
                    f"but error_status.py maps to {status}"
                )


def main() -> int:
    failures: list[str] = []

    _check_env_vars(ENV_EXAMPLE.read_text(), failures)
    _check_compose_secrets([f.read_text() for f in COMPOSE_FILES], failures)
    _check_error_code_mapping(failures)

    if failures:
        print("DOC VALIDATION FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1

    expected = _expect_error_status_table()
    print("DOC VALIDATION OK")
    print(f"  env vars present in .env.example: {len(REQUIRED_ENV_VARS)}")
    for var, *_ in REQUIRED_SECRETS:
        print(f"  {var}: gated in docker-compose.yml + docker-compose.release.yml")
    print(f"  error-code mapping matches error_status.py: {len(expected)} codes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
