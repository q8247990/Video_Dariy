from typing import Any, Optional

from sqlalchemy.orm import Session

from src.core.config import settings
from src.models.mcp_call_log import McpCallLog
from src.services.system_config_registry import MCP_ENABLED, MCP_TOKEN, get_config


def is_mcp_enabled(db: Session) -> bool:
    return bool(get_config(db, MCP_ENABLED))


def get_mcp_token(db: Session) -> str:
    token = str(get_config(db, MCP_TOKEN)).strip()
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
