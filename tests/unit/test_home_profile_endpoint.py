from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from src.api.v1.endpoints.home_profile import (
    create_home_member,
    create_home_pet,
    delete_home_entity,
    get_home_context,
    get_home_entities,
    get_home_profile,
    put_home_profile,
    update_home_entity,
)
from src.models.home_entity_profile import HomeEntityProfile
from src.models.home_profile import HomeProfile
from src.models.system_config import SystemConfig
from src.schemas.home_profile import HomeEntityUpdate, HomeProfileUpsert, MemberCreate, PetCreate


def _new_db_session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    HomeProfile.__table__.create(bind=engine)
    HomeEntityProfile.__table__.create(bind=engine)
    SystemConfig.__table__.create(bind=engine)
    local_session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    return local_session()


def _current_user() -> SimpleNamespace:
    return SimpleNamespace(id=1, username="admin")


def test_home_profile_create_and_update() -> None:
    db = _new_db_session()
    try:
        resp = get_home_profile(db=db, current_user=_current_user())
        assert resp.code == 0
        assert resp.data is not None
        assert resp.data.home_name == "我的家庭"

        update_resp = put_home_profile(
            db=db,
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
    finally:
        db.close()


def test_home_entity_crud_and_context() -> None:
    db = _new_db_session()
    try:
        member_resp = create_home_member(
            db=db,
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
            db=db,
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
            db=db,
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
            db=db,
            current_user=_current_user(),
            locale="zh-CN",
            entity_id=member_resp.data.id,
            payload=HomeEntityUpdate(name="小米米", note="更新备注"),
        )
        assert update_resp.code == 0
        assert update_resp.data is not None
        assert update_resp.data.name == "小米米"

        disable_resp = delete_home_entity(
            db=db,
            current_user=_current_user(),
            locale="zh-CN",
            entity_id=pet_resp.data.id,
        )
        assert disable_resp.code == 0

        list_pets = get_home_entities(
            db=db,
            current_user=_current_user(),
            locale="zh-CN",
            entity_type="pet",
            include_disabled=False,
        )
        assert list_pets.code == 0
        assert list_pets.data is not None
        assert len(list_pets.data) == 0

        context_resp = get_home_context(db=db, current_user=_current_user())
        assert context_resp.code == 0
        assert context_resp.data is not None
        assert len(context_resp.data.members) == 1
        assert len(context_resp.data.pets) == 0
    finally:
        db.close()
