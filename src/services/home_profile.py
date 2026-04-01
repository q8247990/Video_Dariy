from typing import Any, Optional

from sqlalchemy.orm import Session

from src.models.home_entity_profile import HomeEntityProfile
from src.models.home_profile import HomeProfile
from src.schemas.home_profile import (
    AGE_GROUP_OPTIONS,
    ENTITY_TYPE_OPTIONS,
    FAMILY_TAG_OPTIONS,
    FOCUS_POINT_OPTIONS,
    MEMBER_ROLE_OPTIONS,
    PET_ROLE_OPTIONS,
    SYSTEM_STYLE_OPTIONS,
    HomeEntityResponse,
    HomeEntityUpdate,
    HomeProfileResponse,
    HomeProfileUpsert,
)


def get_or_create_home_profile(db: Session) -> HomeProfile:
    profile = db.query(HomeProfile).order_by(HomeProfile.id.asc()).first()
    if profile:
        return profile

    profile = HomeProfile(
        home_name="我的家庭",
        family_tags_json=[],
        focus_points_json=[],
        system_style="family_companion",
        style_preference_text="",
        assistant_name="家庭助手",
        home_note="",
    )
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile


def save_home_profile(db: Session, payload: HomeProfileUpsert) -> HomeProfile:
    profile = get_or_create_home_profile(db)
    profile.home_name = payload.home_name
    profile.family_tags_json = payload.family_tags
    profile.focus_points_json = payload.focus_points
    profile.system_style = payload.system_style
    profile.style_preference_text = payload.style_preference_text
    profile.assistant_name = payload.assistant_name
    profile.home_note = payload.home_note
    db.commit()
    db.refresh(profile)
    return profile


def list_entities(
    db: Session,
    entity_type: Optional[str] = None,
    include_disabled: bool = False,
) -> list[HomeEntityProfile]:
    query = db.query(HomeEntityProfile)
    if entity_type:
        query = query.filter(HomeEntityProfile.entity_type == entity_type)
    if not include_disabled:
        query = query.filter(HomeEntityProfile.is_enabled.is_(True))
    return query.order_by(HomeEntityProfile.sort_order.asc(), HomeEntityProfile.id.asc()).all()


def get_entity_by_id(db: Session, entity_id: int) -> Optional[HomeEntityProfile]:
    return db.query(HomeEntityProfile).filter(HomeEntityProfile.id == entity_id).first()


def create_member(db: Session, payload: dict[str, Any]) -> HomeEntityProfile:
    _validate_role_type("member", payload.get("role_type"))
    _validate_age_group(payload.get("age_group"))
    entity = HomeEntityProfile(entity_type="member", **payload)
    db.add(entity)
    db.commit()
    db.refresh(entity)
    return entity


def create_pet(db: Session, payload: dict[str, Any]) -> HomeEntityProfile:
    _validate_role_type("pet", payload.get("role_type"))
    _validate_age_group(payload.get("age_group"))
    entity = HomeEntityProfile(entity_type="pet", **payload)
    db.add(entity)
    db.commit()
    db.refresh(entity)
    return entity


def update_entity(
    db: Session, entity_id: int, payload: HomeEntityUpdate
) -> Optional[HomeEntityProfile]:
    entity = get_entity_by_id(db, entity_id)
    if entity is None:
        return None

    update_data = payload.model_dump(exclude_unset=True)
    role_type = update_data.get("role_type")
    if role_type is not None:
        _validate_role_type(entity.entity_type, role_type)

    if "age_group" in update_data:
        _validate_age_group(update_data.get("age_group"))

    if entity.entity_type == "member" and "breed" in update_data and update_data.get("breed"):
        raise ValueError("member does not support breed")

    for key, value in update_data.items():
        setattr(entity, key, value)

    db.commit()
    db.refresh(entity)
    return entity


def disable_entity(db: Session, entity_id: int) -> bool:
    entity = get_entity_by_id(db, entity_id)
    if entity is None:
        return False
    entity.is_enabled = False
    db.commit()
    return True


def build_home_context(db: Session) -> dict[str, Any]:
    profile = get_or_create_home_profile(db)
    members = list_entities(db, entity_type="member")
    pets = list_entities(db, entity_type="pet")

    return {
        "home_profile": HomeProfileResponse.model_validate(
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
        ).model_dump(),
        "members": [HomeEntityResponse.model_validate(item).model_dump() for item in members],
        "pets": [HomeEntityResponse.model_validate(item).model_dump() for item in pets],
    }


def get_options() -> dict[str, list[str]]:
    return {
        "family_tags": sorted(FAMILY_TAG_OPTIONS),
        "focus_points": sorted(FOCUS_POINT_OPTIONS),
        "system_styles": sorted(SYSTEM_STYLE_OPTIONS),
        "entity_types": sorted(ENTITY_TYPE_OPTIONS),
        "member_roles": sorted(MEMBER_ROLE_OPTIONS),
        "pet_roles": sorted(PET_ROLE_OPTIONS),
        "age_groups": sorted(AGE_GROUP_OPTIONS),
    }


def _validate_role_type(entity_type: str, role_type: Optional[str]) -> None:
    if not role_type:
        raise ValueError("role_type is required")
    if entity_type == "member" and role_type not in MEMBER_ROLE_OPTIONS:
        raise ValueError("invalid member role_type")
    if entity_type == "pet" and role_type not in PET_ROLE_OPTIONS:
        raise ValueError("invalid pet role_type")


def _validate_age_group(age_group: Optional[str]) -> None:
    if age_group is None:
        return
    if age_group not in AGE_GROUP_OPTIONS:
        raise ValueError("invalid age_group")
