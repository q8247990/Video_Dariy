import base64
import io
import logging
import os
import time
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse

from src.api.deps import DB, CurrentUser, Locale
from src.core.config import settings
from src.core.i18n import t
from src.infrastructure.llm.openai_gateway import OpenAICompatGatewayFactory
from src.models.home_entity_profile import HomeEntityProfile
from src.schemas.home_profile import (
    HomeContextResponse,
    HomeEntityResponse,
    HomeEntityUpdate,
    HomeOptionsResponse,
    HomeProfileResponse,
    HomeProfileUpsert,
    MemberCreate,
    PetCreate,
)
from src.schemas.response import BaseResponse
from src.services.home_profile import (
    build_home_context,
    create_member,
    create_pet,
    disable_entity,
    get_entity_by_id,
    get_options,
    get_or_create_home_profile,
    list_entities,
    save_home_profile,
    update_entity,
)
from src.services.media_signing import MediaCapability, MediaCapabilityError, MediaSigningService
from src.services.provider_key_crypto import decrypt_provider_api_key
from src.services.provider_selector import PROVIDER_TYPE_VISION, find_enabled_provider
from src.services.system_config_registry import HOME_PROFILE_INITIALIZED, set_config

logger = logging.getLogger(__name__)

router = APIRouter()

_ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
_MAX_IMAGE_SIZE = 10 * 1024 * 1024
_IMAGE_MAX_SIDE = 512


def _entity_image_path(entity_id: int) -> str:
    return os.path.join(settings.ENTITY_IMAGE_ROOT, f"entity_{entity_id}.jpg")


def _entity_image_url(entity_id: int) -> str:
    capability = MediaCapability(
        resource_kind="image",
        resource_id=entity_id,
        method="GET",
        expires_at=int(time.time()) + 1_800,
    )
    return MediaSigningService.from_settings().signed_url(
        f"{settings.API_V1_STR}/home-profile/entities/{entity_id}/image", capability
    )


def _build_entity_response(entity: HomeEntityProfile) -> HomeEntityResponse:
    data = HomeEntityResponse.model_validate(entity)
    if entity.image_path and os.path.exists(entity.image_path):
        data.image_url = _entity_image_url(entity.id)
    return data


@router.get("", response_model=BaseResponse[HomeProfileResponse])
def get_home_profile(db: DB, current_user: CurrentUser) -> Any:
    profile = get_or_create_home_profile(db)
    db.commit()
    data = HomeProfileResponse.model_validate(
        {
            "id": profile.id,
            "home_name": profile.home_name,
            "family_tags": profile.family_tags_json or [],
            "focus_points": profile.focus_points_json or [],
            "system_style": profile.system_style,
            "style_preference_text": profile.style_preference_text,
            "assistant_name": profile.assistant_name,
            "home_note": profile.home_note,
            "created_at": profile.created_at,
            "updated_at": profile.updated_at,
        }
    )
    return BaseResponse(data=data)


@router.put("", response_model=BaseResponse[HomeProfileResponse])
def put_home_profile(db: DB, current_user: CurrentUser, payload: HomeProfileUpsert) -> Any:
    profile = save_home_profile(db, payload)
    set_config(db, HOME_PROFILE_INITIALIZED, True)
    db.commit()
    db.refresh(profile)
    data = HomeProfileResponse.model_validate(
        {
            "id": profile.id,
            "home_name": profile.home_name,
            "family_tags": profile.family_tags_json or [],
            "focus_points": profile.focus_points_json or [],
            "system_style": profile.system_style,
            "style_preference_text": profile.style_preference_text,
            "assistant_name": profile.assistant_name,
            "home_note": profile.home_note,
            "created_at": profile.created_at,
            "updated_at": profile.updated_at,
        }
    )
    return BaseResponse(data=data)


@router.get("/entities", response_model=BaseResponse[list[HomeEntityResponse]])
def get_home_entities(
    db: DB,
    current_user: CurrentUser,
    locale: Locale,
    entity_type: Optional[str] = None,
    include_disabled: bool = False,
) -> Any:
    if entity_type is not None and entity_type not in {"member", "pet"}:
        return BaseResponse(code=4000, message=t("entity.invalid_entity_type", locale))
    entities = list_entities(db, entity_type=entity_type, include_disabled=include_disabled)
    data = [_build_entity_response(item) for item in entities]
    return BaseResponse(data=data)


@router.post("/entities/member", response_model=BaseResponse[HomeEntityResponse])
def create_home_member(db: DB, current_user: CurrentUser, payload: MemberCreate) -> Any:
    try:
        entity = create_member(db, payload.model_dump())
        db.commit()
    except ValueError as exc:
        return BaseResponse(code=4001, message=str(exc))
    return BaseResponse(data=_build_entity_response(entity))


@router.post("/entities/pet", response_model=BaseResponse[HomeEntityResponse])
def create_home_pet(db: DB, current_user: CurrentUser, payload: PetCreate) -> Any:
    try:
        entity = create_pet(db, payload.model_dump())
        db.commit()
    except ValueError as exc:
        return BaseResponse(code=4001, message=str(exc))
    return BaseResponse(data=_build_entity_response(entity))


@router.put("/entities/{entity_id}", response_model=BaseResponse[HomeEntityResponse])
def update_home_entity(
    db: DB,
    current_user: CurrentUser,
    locale: Locale,
    entity_id: int,
    payload: HomeEntityUpdate,
) -> Any:
    try:
        entity = update_entity(db, entity_id, payload)
        db.commit()
    except ValueError as exc:
        return BaseResponse(code=4001, message=str(exc))

    if entity is None:
        return BaseResponse(code=4002, message=t("entity.not_found", locale))
    return BaseResponse(data=_build_entity_response(entity))


@router.delete("/entities/{entity_id}", response_model=BaseResponse[dict])
def delete_home_entity(db: DB, current_user: CurrentUser, locale: Locale, entity_id: int) -> Any:
    ok = disable_entity(db, entity_id)
    db.commit()
    if not ok:
        return BaseResponse(code=4002, message=t("entity.not_found", locale))
    return BaseResponse(data={})


@router.get("/entities/{entity_id}/image")
def get_entity_image(
    entity_id: int, db: DB, locale: Locale, token: str | None = Query(default=None)
) -> Any:
    if token is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing media capability"
        )
    expected = MediaCapability(
        resource_kind="image", resource_id=entity_id, method="GET", expires_at=0
    )
    try:
        MediaSigningService.from_settings().verify(token, expected)
    except MediaCapabilityError as error:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Invalid media capability"
        ) from error
    entity = get_entity_by_id(db, entity_id)
    if entity is None:
        return BaseResponse(code=4002, message=t("entity.not_found", locale))
    image_path = _entity_image_path(entity_id)
    if not entity.image_path or not os.path.exists(image_path):
        return BaseResponse(code=4004, message=t("entity.no_image", locale))
    return FileResponse(image_path, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@router.post("/entities/{entity_id}/image", response_model=BaseResponse[HomeEntityResponse])
async def upload_entity_image(
    entity_id: int, db: DB, current_user: CurrentUser, locale: Locale, file: UploadFile
) -> Any:
    from PIL import Image

    entity = get_entity_by_id(db, entity_id)
    if entity is None:
        return BaseResponse(code=4002, message=t("entity.not_found", locale))

    if file.content_type not in _ALLOWED_IMAGE_TYPES:
        return BaseResponse(code=4000, message=t("entity.unsupported_image_format", locale))

    raw = await file.read()
    if len(raw) > _MAX_IMAGE_SIZE:
        return BaseResponse(code=4000, message=t("entity.image_too_large", locale))

    try:
        img = Image.open(io.BytesIO(raw)).convert("RGB")
        w, h = img.size
        if max(w, h) > _IMAGE_MAX_SIDE:
            ratio = _IMAGE_MAX_SIDE / max(w, h)
            img = img.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)

        os.makedirs(settings.ENTITY_IMAGE_ROOT, exist_ok=True)
        save_path = _entity_image_path(entity_id)
        img.save(save_path, "JPEG", quality=85)
    except Exception as exc:
        logger.error("Failed to process image for entity %s: %s", entity_id, exc)
        return BaseResponse(code=5000, message=t("entity.image_process_failed", locale))

    entity.image_path = save_path
    db.commit()
    db.refresh(entity)
    return BaseResponse(data=_build_entity_response(entity))


@router.delete("/entities/{entity_id}/image", response_model=BaseResponse[HomeEntityResponse])
def delete_entity_image(entity_id: int, db: DB, current_user: CurrentUser, locale: Locale) -> Any:
    entity = get_entity_by_id(db, entity_id)
    if entity is None:
        return BaseResponse(code=4002, message=t("entity.not_found", locale))

    if entity.image_path and os.path.exists(entity.image_path):
        try:
            os.remove(entity.image_path)
        except OSError as exc:
            logger.warning("Failed to delete image file for entity %s: %s", entity_id, exc)

    entity.image_path = None
    db.commit()
    db.refresh(entity)
    return BaseResponse(data=_build_entity_response(entity))


@router.post(
    "/entities/{entity_id}/generate-appearance",
    response_model=BaseResponse[HomeEntityResponse],
)
def generate_entity_appearance(
    entity_id: int,
    db: DB,
    current_user: CurrentUser,
    locale: Locale,
) -> Any:
    entity = get_entity_by_id(db, entity_id)
    if entity is None:
        return BaseResponse(code=4002, message=t("entity.not_found", locale))

    image_path = _entity_image_path(entity_id)
    if not entity.image_path or not os.path.exists(image_path):
        return BaseResponse(code=4000, message=t("entity.upload_image_first", locale))

    provider = find_enabled_provider(db, PROVIDER_TYPE_VISION)
    if provider is None:
        return BaseResponse(code=5000, message=t("entity.no_vision_provider", locale))

    try:
        with open(image_path, "rb") as f:
            image_b64 = base64.b64encode(f.read()).decode("utf-8")
        data_url = f"data:image/jpeg;base64,{image_b64}"
    except OSError as exc:
        logger.error("Failed to read image for entity %s: %s", entity_id, exc)
        return BaseResponse(code=5000, message=t("entity.image_read_failed", locale))

    entity_label = "宠物" if entity.entity_type == "pet" else "家庭成员"
    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": (
                        f"请仔细观察图片中的{entity_label}，用中文描述其外观特征。"
                        f"描述应包括：体型、毛发/发型发色、面部特征、常见穿着风格等可观察到的外观信息。"
                        f"只描述外观，不要推测性格或行为。"
                        f"描述控制在 150 字以内，语言简洁自然。"
                    ),
                },
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        }
    ]

    try:
        gateway = OpenAICompatGatewayFactory().build(
            api_base_url=provider.api_base_url,
            api_key=decrypt_provider_api_key(provider.api_key),
            model_name=provider.model_name,
            timeout_seconds=provider.timeout_seconds,
        )
        result = gateway.chat_completion(messages=messages, temperature=0.3, max_tokens=300)
    except Exception as exc:
        logger.error("Vision LLM call failed for entity %s: %s", entity_id, exc)
        return BaseResponse(code=5002, message=t("entity.ai_generate_failed", locale, error=exc))

    if not result:
        return BaseResponse(code=5002, message=t("entity.ai_no_result", locale))

    entity.appearance_desc = result.strip()
    db.commit()
    db.refresh(entity)
    return BaseResponse(data=_build_entity_response(entity))


@router.get("/context", response_model=BaseResponse[HomeContextResponse])
def get_home_context(db: DB, current_user: CurrentUser) -> Any:
    context = build_home_context(db)
    db.commit()
    return BaseResponse(data=HomeContextResponse.model_validate(context))


@router.get("/options", response_model=BaseResponse[HomeOptionsResponse])
def get_home_options(db: DB, current_user: CurrentUser) -> Any:
    options = get_options()
    return BaseResponse(data=HomeOptionsResponse.model_validate(options))
