import pytest
from pydantic import ValidationError

from src.schemas.home_profile import HomeProfileUpsert, MemberCreate, PetCreate


def test_home_profile_upsert_rejects_invalid_system_style() -> None:
    with pytest.raises(ValidationError):
        HomeProfileUpsert(
            home_name="我的家庭",
            family_tags=["has_pet"],
            focus_points=["pet_status"],
            system_style="invalid_style",
            assistant_name="家庭助手",
        )


def test_member_create_rejects_invalid_role() -> None:
    with pytest.raises(ValidationError):
        MemberCreate(
            name="小米",
            role_type="cat",
            age_group="child",
            sort_order=0,
            is_enabled=True,
        )


def test_pet_create_accepts_valid_payload() -> None:
    payload = PetCreate(
        name="布丁",
        role_type="cat",
        breed="橘猫",
        sort_order=1,
        is_enabled=True,
    )

    assert payload.role_type == "cat"
    assert payload.breed == "橘猫"
