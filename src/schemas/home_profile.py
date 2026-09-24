import re
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

FAMILY_TAG_OPTIONS = {"has_pet", "has_child", "has_elder"}
SYSTEM_STYLE_OPTIONS = {"concise_summary", "family_companion", "focus_alert"}

MAX_FOCUS_POINTS = 15
FOCUS_POINT_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

FOCUS_POINT_PRESETS: list[dict[str, object]] = [
    {"key": "pet_status", "label": "宠物状态", "description": "宠物的活动、进食、休息或异常"},
    {"key": "member_inout", "label": "成员进出", "description": "家庭成员回家、离家或进出画面"},
    {
        "key": "stranger_or_stay",
        "label": "陌生人与异常停留",
        "description": "陌生人出现或人员异常停留",
    },
    {
        "key": "elder_safety",
        "label": "老人安全",
        "description": "老人跌倒、长时间无活动等安全相关",
    },
    {"key": "child_activity", "label": "孩子活动", "description": "孩子的活动与在家情况"},
    {"key": "daily_summary", "label": "日常总览", "description": "当日整体情况概览"},
]

_PRESET_BY_KEY: dict[str, dict[str, object]] = {
    str(item["key"]): item for item in FOCUS_POINT_PRESETS
}


class FocusPointItem(BaseModel):
    key: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=20)
    description: str = Field(default="", max_length=100)
    enabled: bool = True
    attention: bool = False

    @field_validator("key")
    @classmethod
    def validate_key(cls, value: str) -> str:
        key = value.strip().lower()
        if not FOCUS_POINT_KEY_PATTERN.match(key):
            raise ValueError("invalid focus point key")
        return key

    @field_validator("label")
    @classmethod
    def validate_label(cls, value: str) -> str:
        label = value.strip()
        if not label:
            raise ValueError("focus point label is required")
        return label

    @field_validator("description")
    @classmethod
    def validate_description(cls, value: str) -> str:
        return (value or "").strip()


def _coerce_focus_point(item: object) -> object:
    if isinstance(item, str):
        preset = _PRESET_BY_KEY.get(item.strip().lower())
        return dict(preset) if preset else {"key": item, "label": item, "description": ""}
    return item


def coerce_focus_points(raw: object) -> list[FocusPointItem]:
    if not isinstance(raw, list):
        return []
    items: list[FocusPointItem] = []
    for entry in raw:
        try:
            items.append(FocusPointItem.model_validate(_coerce_focus_point(entry)))
        except ValidationError:
            continue
    return items


ENTITY_TYPE_OPTIONS = {"member", "pet"}
MEMBER_ROLE_OPTIONS = {"father", "mother", "child", "elder", "other_member"}
PET_ROLE_OPTIONS = {"cat", "dog", "other_pet"}
AGE_GROUP_OPTIONS = {"child", "adult", "elder"}


def _validate_set_items(values: list[str], options: set[str], field_name: str) -> list[str]:
    invalid_values = [item for item in values if item not in options]
    if invalid_values:
        raise ValueError(f"{field_name} contains invalid values: {', '.join(invalid_values)}")
    return values


class HomeProfileUpsert(BaseModel):
    home_name: str = Field(default="我的家庭", min_length=1, max_length=128)
    family_tags: list[str] = Field(default_factory=list)
    focus_points: list[FocusPointItem] = Field(default_factory=list)
    system_style: str = Field(default="family_companion", min_length=1, max_length=32)
    style_preference_text: Optional[str] = Field(default=None, max_length=1000)
    assistant_name: str = Field(default="家庭助手", min_length=1, max_length=128)
    home_note: Optional[str] = Field(default=None, max_length=1000)

    @field_validator("family_tags")
    @classmethod
    def validate_family_tags(cls, values: list[str]) -> list[str]:
        return _validate_set_items(values, FAMILY_TAG_OPTIONS, "family_tags")

    @field_validator("focus_points", mode="before")
    @classmethod
    def coerce_focus_points(cls, values: object) -> object:
        if not isinstance(values, list):
            return values
        return [_coerce_focus_point(item) for item in values]

    @field_validator("focus_points")
    @classmethod
    def validate_focus_points(cls, values: list[FocusPointItem]) -> list[FocusPointItem]:
        if len(values) > MAX_FOCUS_POINTS:
            raise ValueError(f"focus_points exceeds {MAX_FOCUS_POINTS} items")
        keys = [item.key for item in values]
        if len(set(keys)) != len(keys):
            raise ValueError("focus_points contains duplicate keys")
        return values

    @field_validator("system_style")
    @classmethod
    def validate_system_style(cls, value: str) -> str:
        if value not in SYSTEM_STYLE_OPTIONS:
            raise ValueError("invalid system_style")
        return value


class HomeProfileResponse(HomeProfileUpsert):
    id: int
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class HomeEntityBase(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    role_type: str = Field(min_length=1, max_length=64)
    age_group: Optional[str] = Field(default=None, max_length=32)
    breed: Optional[str] = Field(default=None, max_length=128)
    appearance_desc: Optional[str] = Field(default=None, max_length=1000)
    personality_desc: Optional[str] = Field(default=None, max_length=1000)
    note: Optional[str] = Field(default=None, max_length=1000)
    sort_order: int = Field(default=0, ge=0, le=9999)
    is_enabled: bool = True

    @field_validator("age_group")
    @classmethod
    def validate_age_group(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        if value not in AGE_GROUP_OPTIONS:
            raise ValueError("invalid age_group")
        return value


class MemberCreate(HomeEntityBase):
    @field_validator("role_type")
    @classmethod
    def validate_role_type(cls, value: str) -> str:
        if value not in MEMBER_ROLE_OPTIONS:
            raise ValueError("invalid member role_type")
        return value


class PetCreate(HomeEntityBase):
    @field_validator("role_type")
    @classmethod
    def validate_role_type(cls, value: str) -> str:
        if value not in PET_ROLE_OPTIONS:
            raise ValueError("invalid pet role_type")
        return value


class HomeEntityUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=128)
    role_type: Optional[str] = Field(default=None, min_length=1, max_length=64)
    age_group: Optional[str] = Field(default=None, max_length=32)
    breed: Optional[str] = Field(default=None, max_length=128)
    appearance_desc: Optional[str] = Field(default=None, max_length=1000)
    personality_desc: Optional[str] = Field(default=None, max_length=1000)
    note: Optional[str] = Field(default=None, max_length=1000)
    sort_order: Optional[int] = Field(default=None, ge=0, le=9999)
    is_enabled: Optional[bool] = None

    @field_validator("age_group")
    @classmethod
    def validate_age_group(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        if value not in AGE_GROUP_OPTIONS:
            raise ValueError("invalid age_group")
        return value


class HomeEntityResponse(BaseModel):
    id: int
    entity_type: str
    name: str
    role_type: str
    age_group: Optional[str] = None
    breed: Optional[str] = None
    appearance_desc: Optional[str] = None
    personality_desc: Optional[str] = None
    note: Optional[str] = None
    sort_order: int
    is_enabled: bool
    image_url: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class HomeContextResponse(BaseModel):
    home_profile: HomeProfileResponse
    members: list[HomeEntityResponse]
    pets: list[HomeEntityResponse]


class HomeOptionsResponse(BaseModel):
    family_tags: list[str]
    focus_points: list[FocusPointItem]
    system_styles: list[str]
    entity_types: list[str]
    member_roles: list[str]
    pet_roles: list[str]
    age_groups: list[str]
