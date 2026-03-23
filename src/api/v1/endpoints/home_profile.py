from typing import Any, Optional

from fastapi import APIRouter

from src.api.deps import DB, CurrentUser
from src.models.system_config import SystemConfig
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
    get_options,
    get_or_create_home_profile,
    list_entities,
    save_home_profile,
    update_entity,
)

router = APIRouter()


@router.get("", response_model=BaseResponse[HomeProfileResponse])
def get_home_profile(db: DB, current_user: CurrentUser) -> Any:
    profile = get_or_create_home_profile(db)
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
    initialized_key = "home_profile_initialized"
    initialized_config = (
        db.query(SystemConfig).filter(SystemConfig.config_key == initialized_key).first()
    )
    if initialized_config is None:
        db.add(SystemConfig(config_key=initialized_key, config_value=True))
    else:
        initialized_config.config_value = True
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
    entity_type: Optional[str] = None,
    include_disabled: bool = False,
) -> Any:
    if entity_type is not None and entity_type not in {"member", "pet"}:
        return BaseResponse(code=4000, message="invalid entity_type")
    entities = list_entities(db, entity_type=entity_type, include_disabled=include_disabled)
    data = [HomeEntityResponse.model_validate(item) for item in entities]
    return BaseResponse(data=data)


@router.post("/entities/member", response_model=BaseResponse[HomeEntityResponse])
def create_home_member(db: DB, current_user: CurrentUser, payload: MemberCreate) -> Any:
    try:
        entity = create_member(db, payload.model_dump())
    except ValueError as exc:
        return BaseResponse(code=4001, message=str(exc))
    return BaseResponse(data=HomeEntityResponse.model_validate(entity))


@router.post("/entities/pet", response_model=BaseResponse[HomeEntityResponse])
def create_home_pet(db: DB, current_user: CurrentUser, payload: PetCreate) -> Any:
    try:
        entity = create_pet(db, payload.model_dump())
    except ValueError as exc:
        return BaseResponse(code=4001, message=str(exc))
    return BaseResponse(data=HomeEntityResponse.model_validate(entity))


@router.put("/entities/{entity_id}", response_model=BaseResponse[HomeEntityResponse])
def update_home_entity(
    db: DB,
    current_user: CurrentUser,
    entity_id: int,
    payload: HomeEntityUpdate,
) -> Any:
    try:
        entity = update_entity(db, entity_id, payload)
    except ValueError as exc:
        return BaseResponse(code=4001, message=str(exc))

    if entity is None:
        return BaseResponse(code=4002, message="entity not found")
    return BaseResponse(data=HomeEntityResponse.model_validate(entity))


@router.delete("/entities/{entity_id}", response_model=BaseResponse[dict])
def delete_home_entity(db: DB, current_user: CurrentUser, entity_id: int) -> Any:
    ok = disable_entity(db, entity_id)
    if not ok:
        return BaseResponse(code=4002, message="entity not found")
    return BaseResponse(data={})


@router.get("/context", response_model=BaseResponse[HomeContextResponse])
def get_home_context(db: DB, current_user: CurrentUser) -> Any:
    context = build_home_context(db)
    return BaseResponse(data=HomeContextResponse.model_validate(context))


@router.get("/options", response_model=BaseResponse[HomeOptionsResponse])
def get_home_options(db: DB, current_user: CurrentUser) -> Any:
    options = get_options()
    return BaseResponse(data=HomeOptionsResponse.model_validate(options))
