from datetime import date, datetime
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from src.models.llm_provider import LLMProvider
from src.models.llm_usage_log import LLMUsageLog
from src.models.system_config import SystemConfig

GLOBAL_DAILY_TOKEN_QUOTA_KEY = "llm_daily_token_quota_global"
PROVIDER_DAILY_TOKEN_QUOTA_KEY = "llm_daily_token_quota_per_provider"


def provider_availability(provider: LLMProvider) -> tuple[str, str]:
    if not provider.enabled:
        return "unavailable", "provider disabled"

    if provider.last_test_status == "success":
        return "available", provider.last_test_message or "ok"

    if provider.last_test_status == "failed":
        return "degraded", provider.last_test_message or "last test failed"

    return "unknown", "never tested"


def enforce_token_quota(db: Session, provider: LLMProvider, now: datetime | None = None) -> None:
    current_time = now or datetime.utcnow()
    usage_date = current_time.date()

    global_quota = _get_int_config(db, GLOBAL_DAILY_TOKEN_QUOTA_KEY)
    if global_quota > 0:
        today_total = _sum_tokens(db, usage_date=usage_date)
        if today_total >= global_quota:
            raise ValueError("Global daily token quota exceeded")

    provider_quota = _provider_daily_quota(db, provider)
    if provider_quota > 0:
        provider_total = _sum_tokens(db, usage_date=usage_date, provider_id=provider.id)
        if provider_total >= provider_quota:
            raise ValueError(f"Provider {provider.id} daily token quota exceeded")


def record_token_usage(
    db: Session,
    *,
    provider_id: int,
    provider_name_snapshot: str | None = None,
    scene: str,
    usage: dict[str, Any] | None,
    now: datetime | None = None,
) -> None:
    if not usage:
        return

    prompt_tokens = _to_int(usage.get("prompt_tokens"))
    completion_tokens = _to_int(usage.get("completion_tokens"))
    total_tokens = _to_int(usage.get("total_tokens"))
    if total_tokens <= 0:
        total_tokens = prompt_tokens + completion_tokens
    if total_tokens <= 0:
        return

    usage_date = (now or datetime.utcnow()).date()
    db.add(
        LLMUsageLog(
            provider_id=provider_id,
            provider_name_snapshot=provider_name_snapshot,
            usage_date=usage_date,
            scene=scene,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
        )
    )


def get_daily_usage_stats(db: Session, days: int = 7) -> list[dict[str, Any]]:
    today = datetime.utcnow().date()
    start_date = today.fromordinal(today.toordinal() - max(days - 1, 0))
    rows = (
        db.query(
            LLMUsageLog.usage_date,
            LLMUsageLog.provider_id,
            LLMUsageLog.provider_name_snapshot,
            func.sum(LLMUsageLog.prompt_tokens),
            func.sum(LLMUsageLog.completion_tokens),
            func.sum(LLMUsageLog.total_tokens),
        )
        .filter(LLMUsageLog.usage_date >= start_date)
        .group_by(
            LLMUsageLog.usage_date,
            LLMUsageLog.provider_id,
            LLMUsageLog.provider_name_snapshot,
        )
        .all()
    )

    providers = {item.id: item.provider_name for item in db.query(LLMProvider).all()}
    per_day: dict[date, dict[str, Any]] = {}
    for (
        usage_date,
        provider_id,
        provider_name_snapshot,
        prompt_sum,
        completion_sum,
        total_sum,
    ) in rows:
        day = per_day.setdefault(
            usage_date,
            {
                "date": usage_date.isoformat(),
                "total_tokens": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "providers": [],
            },
        )
        prompt_value = _to_int(prompt_sum)
        completion_value = _to_int(completion_sum)
        total_value = _to_int(total_sum)
        day["prompt_tokens"] += prompt_value
        day["completion_tokens"] += completion_value
        day["total_tokens"] += total_value
        day["providers"].append(
            {
                "provider_id": provider_id,
                "provider_name": provider_name_snapshot
                or providers.get(provider_id)
                or (f"provider-{provider_id}" if provider_id is not None else "deleted-provider"),
                "prompt_tokens": prompt_value,
                "completion_tokens": completion_value,
                "total_tokens": total_value,
            }
        )

    return [per_day[key] for key in sorted(per_day.keys(), reverse=True)]


def _to_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _get_int_config(db: Session, key: str) -> int:
    row = db.query(SystemConfig).filter(SystemConfig.config_key == key).first()
    if row is None:
        return 0
    return _to_int(row.config_value)


def _sum_tokens(db: Session, *, usage_date: date, provider_id: int | None = None) -> int:
    query = db.query(func.sum(LLMUsageLog.total_tokens)).filter(
        LLMUsageLog.usage_date == usage_date
    )
    if provider_id is not None:
        query = query.filter(LLMUsageLog.provider_id == provider_id)
    value = query.scalar()
    return _to_int(value)


def _provider_daily_quota(db: Session, provider: LLMProvider) -> int:
    extra = provider.extra_config_json or {}
    if isinstance(extra, dict) and "daily_token_quota" in extra:
        return _to_int(extra.get("daily_token_quota"))
    return _get_int_config(db, PROVIDER_DAILY_TOKEN_QUOTA_KEY)
