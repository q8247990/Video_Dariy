from typing import Any, Optional

from sqlalchemy.orm import Session

from src.core.config import settings
from src.models.mcp_call_log import McpCallLog
from src.models.system_config import SystemConfig

MCP_CONFIG_KEY_ENABLED = "mcp_enabled"
MCP_CONFIG_KEY_TOKEN = "mcp_token"


def to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(value, (int, float)):
        return bool(value)
    return False


def is_mcp_enabled(db: Session) -> bool:
    row = db.query(SystemConfig).filter(SystemConfig.config_key == MCP_CONFIG_KEY_ENABLED).first()
    if row is None or row.config_value is None:
        return True
    return to_bool(row.config_value)


def get_mcp_token(db: Session) -> str:
    row = db.query(SystemConfig).filter(SystemConfig.config_key == MCP_CONFIG_KEY_TOKEN).first()
    if row is None or row.config_value is None:
        return settings.MCP_TOKEN
    token = str(row.config_value).strip()
    if not token:
        return settings.MCP_TOKEN
    return token


def is_token_valid(db: Session, token: Optional[str]) -> bool:
    if not token:
        return False
    return token.strip() == get_mcp_token(db)


def authorize(db: Session, token: Optional[str]) -> Optional[str]:
    if not is_mcp_enabled(db):
        return "mcp service is disabled"
    if not is_token_valid(db, token):
        return "invalid mcp token"
    return None


def log_mcp_call(
    db: Session,
    tool_name: str,
    request_json: dict[str, Any],
    response_json: dict[str, Any],
    status: str,
) -> None:
    try:
        db.add(
            McpCallLog(
                tool_name=tool_name,
                request_json=request_json,
                response_json=response_json,
                status=status,
            )
        )
        db.commit()
    except Exception:
        db.rollback()
