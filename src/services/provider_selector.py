from sqlalchemy.orm import Session

from src.models.llm_provider import LLMProvider

PROVIDER_TYPE_VISION = "vision_provider"
PROVIDER_TYPE_QA = "qa_provider"

_PROVIDER_TYPE_ALIASES = {
    PROVIDER_TYPE_VISION: PROVIDER_TYPE_VISION,
    "visionprovider": PROVIDER_TYPE_VISION,
    PROVIDER_TYPE_QA: PROVIDER_TYPE_QA,
    "qaprovider": PROVIDER_TYPE_QA,
    "aqprovider": PROVIDER_TYPE_QA,
}

_PROVIDER_TYPE_TO_CAPABILITY = {
    PROVIDER_TYPE_VISION: "supports_vision",
    PROVIDER_TYPE_QA: "supports_qa",
}

_PROVIDER_TYPE_TO_DEFAULT = {
    PROVIDER_TYPE_VISION: "is_default_vision",
    PROVIDER_TYPE_QA: "is_default_qa",
}


def normalize_provider_type(provider_type: str) -> str:
    normalized = _PROVIDER_TYPE_ALIASES.get(provider_type.strip().lower())
    if not normalized:
        raise ValueError(f"Unsupported provider_type: {provider_type}")
    return normalized


def capability_field_for_provider_type(provider_type: str) -> str:
    normalized_type = normalize_provider_type(provider_type)
    return _PROVIDER_TYPE_TO_CAPABILITY[normalized_type]


def default_field_for_provider_type(provider_type: str) -> str:
    normalized_type = normalize_provider_type(provider_type)
    return _PROVIDER_TYPE_TO_DEFAULT[normalized_type]


def find_enabled_provider(db: Session, provider_type: str) -> LLMProvider | None:
    capability_field = capability_field_for_provider_type(provider_type)
    default_field = default_field_for_provider_type(provider_type)
    capability_column = getattr(LLMProvider, capability_field)
    default_column = getattr(LLMProvider, default_field)
    provider = (
        db.query(LLMProvider)
        .filter(
            capability_column.is_(True),
            LLMProvider.enabled.is_(True),
            default_column.is_(True),
        )
        .first()
    )
    if provider:
        return provider

    return (
        db.query(LLMProvider)
        .filter(capability_column.is_(True), LLMProvider.enabled.is_(True))
        .first()
    )


def find_required_enabled_provider(db: Session, provider_type: str) -> LLMProvider:
    provider = find_enabled_provider(db, provider_type)
    normalized_type = normalize_provider_type(provider_type)
    if not provider:
        raise ValueError(f"No enabled {normalized_type} found")
    return provider
