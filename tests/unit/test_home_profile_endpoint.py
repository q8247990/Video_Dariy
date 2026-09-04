from types import SimpleNamespace

from sqlalchemy.orm import Session

from src.api.v1.endpoints.home_profile import (
    create_home_member,
    create_home_pet,
    delete_home_entity,
    get_home_entities,
    get_home_profile,
    put_home_profile,
    update_home_entity,
)
from src.schemas.home_profile import HomeEntityUpdate, HomeProfileUpsert, MemberCreate, PetCreate


def _current_user() -> SimpleNamespace:
    return SimpleNamespace(id=1, username="admin")


def test_home_profile_create_and_update(pg_db: Session) -> None:
    resp = get_home_profile(db=pg_db, current_user=_current_user())
    assert resp.code == 0
    assert resp.data is not None
    assert resp.data.home_name == "我的家庭"

    update_resp = put_home_profile(
        db=pg_db,
        current_user=_current_user(),
        payload=HomeProfileUpsert(
            home_name="小王一家",
            family_tags=["has_pet"],
            focus_points=["pet_status"],
            system_style="family_companion",
            style_preference_text="描述简洁一点",
            assistant_name="小布",
            home_note="白天较安静",
        ),
    )
    assert update_resp.code == 0
    assert update_resp.data is not None
    assert update_resp.data.home_name == "小王一家"
    assert update_resp.data.assistant_name == "小布"


def test_home_entity_crud(pg_db: Session) -> None:
    member_resp = create_home_member(
        db=pg_db,
        current_user=_current_user(),
        payload=MemberCreate(
            name="小米",
            role_type="child",
            age_group="child",
            appearance_desc="短发",
            note="客厅活动",
            sort_order=1,
            is_enabled=True,
        ),
    )
    assert member_resp.code == 0
    assert member_resp.data is not None

    pet_resp = create_home_pet(
        db=pg_db,
        current_user=_current_user(),
        payload=PetCreate(
            name="布丁",
            role_type="cat",
            breed="橘猫",
            personality_desc="平时安静",
            sort_order=2,
            is_enabled=True,
        ),
    )
    assert pet_resp.code == 0
    assert pet_resp.data is not None

    list_members = get_home_entities(
        db=pg_db,
        current_user=_current_user(),
        locale="zh-CN",
        entity_type="member",
        include_disabled=False,
    )
    assert list_members.code == 0
    assert list_members.data is not None
    assert len(list_members.data) == 1
    assert list_members.data[0].name == "小米"

    update_resp = update_home_entity(
        db=pg_db,
        current_user=_current_user(),
        locale="zh-CN",
        entity_id=member_resp.data.id,
        payload=HomeEntityUpdate(name="小米米", note="更新备注"),
    )
    assert update_resp.code == 0
    assert update_resp.data is not None
    assert update_resp.data.name == "小米米"

    disable_resp = delete_home_entity(
        db=pg_db,
        current_user=_current_user(),
        locale="zh-CN",
        entity_id=pet_resp.data.id,
    )
    assert disable_resp.code == 0

    list_pets = get_home_entities(
        db=pg_db,
        current_user=_current_user(),
        locale="zh-CN",
        entity_type="pet",
        include_disabled=False,
    )
    assert list_pets.code == 0
    assert list_pets.data is not None
    assert len(list_pets.data) == 0
