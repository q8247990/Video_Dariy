"""Tests verifying service layer uses flush (not commit) for transaction composition."""

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from src.models.home_entity_profile import HomeEntityProfile
from src.models.home_profile import HomeProfile
from src.models.task_log import TaskLog
from src.schemas.home_profile import HomeEntityUpdate, HomeProfileUpsert
from src.services.home_profile import (
    create_member,
    create_pet,
    disable_entity,
    get_or_create_home_profile,
    save_home_profile,
    update_entity,
)
from src.services.task_dispatch_control import create_pending_task_log


def _make_db() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    HomeProfile.__table__.create(bind=engine)
    HomeEntityProfile.__table__.create(bind=engine)
    TaskLog.__table__.create(bind=engine)
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    return factory()


def test_get_or_create_home_profile_flush_is_rollbackable() -> None:
    db = _make_db()
    try:
        profile = get_or_create_home_profile(db)
        assert profile.id is not None

        db.rollback()
        assert db.query(HomeProfile).count() == 0
    finally:
        db.close()


def test_get_or_create_home_profile_persists_after_caller_commit() -> None:
    db = _make_db()
    try:
        profile = get_or_create_home_profile(db)
        db.commit()

        db.expire_all()
        assert db.query(HomeProfile).count() == 1
        assert db.query(HomeProfile).first().home_name == profile.home_name
    finally:
        db.close()


def test_save_home_profile_flush_is_rollbackable() -> None:
    db = _make_db()
    try:
        get_or_create_home_profile(db)
        db.commit()

        payload = HomeProfileUpsert(
            home_name="new_name",
            family_tags=["has_pet"],
            focus_points=[],
            system_style="family_companion",
            style_preference_text="",
            assistant_name="bot",
            home_note="",
        )
        save_home_profile(db, payload)

        db.rollback()
        db.expire_all()
        assert db.query(HomeProfile).first().home_name == "\u6211\u7684\u5bb6\u5ead"
    finally:
        db.close()


def test_create_member_flush_is_rollbackable() -> None:
    db = _make_db()
    try:
        entity = create_member(
            db,
            {
                "name": "test_member",
                "role_type": "child",
                "age_group": "child",
            },
        )
        assert entity.id is not None

        db.rollback()
        assert db.query(HomeEntityProfile).count() == 0
    finally:
        db.close()


def test_create_pet_flush_is_rollbackable() -> None:
    db = _make_db()
    try:
        entity = create_pet(
            db,
            {
                "name": "test_pet",
                "role_type": "cat",
                "age_group": "adult",
            },
        )
        assert entity.id is not None

        db.rollback()
        assert db.query(HomeEntityProfile).count() == 0
    finally:
        db.close()


def test_update_entity_flush_is_rollbackable() -> None:
    db = _make_db()
    try:
        entity = create_member(
            db,
            {
                "name": "original",
                "role_type": "child",
                "age_group": "child",
            },
        )
        db.commit()

        updated = update_entity(db, entity.id, HomeEntityUpdate(name="changed"))
        assert updated is not None
        assert updated.name == "changed"

        db.rollback()
        db.expire_all()
        refreshed = db.query(HomeEntityProfile).filter(HomeEntityProfile.id == entity.id).first()
        assert refreshed.name == "original"
    finally:
        db.close()


def test_disable_entity_flush_is_rollbackable() -> None:
    db = _make_db()
    try:
        entity = create_member(
            db,
            {
                "name": "test",
                "role_type": "child",
                "age_group": "child",
            },
        )
        db.commit()

        assert disable_entity(db, entity.id) is True

        db.rollback()
        db.expire_all()
        refreshed = db.query(HomeEntityProfile).filter(HomeEntityProfile.id == entity.id).first()
        assert refreshed.is_enabled is True
    finally:
        db.close()


def test_create_pending_task_log_flush_is_rollbackable() -> None:
    db = _make_db()
    try:
        task_log, created = create_pending_task_log(
            db,
            task_type="session_build",
            task_target_id=1,
            detail_json={"scan_mode": "hot"},
        )
        assert created is True
        assert task_log.id is not None

        db.rollback()
        assert db.query(TaskLog).count() == 0
    finally:
        db.close()


def test_transaction_composition_rollback_reverts_all() -> None:
    db = _make_db()
    try:
        get_or_create_home_profile(db)
        create_member(
            db,
            {
                "name": "member1",
                "role_type": "child",
                "age_group": "child",
            },
        )

        db.rollback()
        assert db.query(HomeProfile).count() == 0
        assert db.query(HomeEntityProfile).count() == 0
    finally:
        db.close()


def test_transaction_composition_commit_persists_all() -> None:
    db = _make_db()
    try:
        get_or_create_home_profile(db)
        create_member(
            db,
            {
                "name": "member1",
                "role_type": "child",
                "age_group": "child",
            },
        )
        db.commit()

        db.expire_all()
        assert db.query(HomeProfile).count() == 1
        assert db.query(HomeEntityProfile).count() == 1
    finally:
        db.close()
