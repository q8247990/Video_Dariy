"""Tests verifying service layer uses flush (not commit) for transaction composition.

PostgreSQL: the ``pg_db`` fixture wraps every test in an outer
transaction. ``commit()`` here is a savepoint release (the data stays
in the outer transaction until the fixture rolls back at teardown),
and ``rollback()`` would roll back the entire outer transaction
— including setup rows that a prior ``commit()`` thought it had
persisted. To keep the SQLite-era "commit → flush a change → rollback
reverts only the change" semantics we need an explicit ``SAVEPOINT``
around the post-commit work, so we use ``begin_nested()`` +
``nested.rollback()`` for the savepoint; the outer ``pg_db.commit()``
and ``pg_db.rollback()`` calls below stay load-bearing because they
verify the fixture-level contracts (commit makes writes visible
within the session, rollback reverts them).
"""

from sqlalchemy.orm import Session

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


def test_get_or_create_home_profile_flush_is_rollbackable(pg_db: Session) -> None:
    profile = get_or_create_home_profile(pg_db)
    assert profile.id is not None

    pg_db.rollback()
    assert pg_db.query(HomeProfile).count() == 0


def test_get_or_create_home_profile_persists_after_caller_commit(pg_db: Session) -> None:
    profile = get_or_create_home_profile(pg_db)
    pg_db.commit()

    pg_db.expire_all()
    assert pg_db.query(HomeProfile).count() == 1
    assert pg_db.query(HomeProfile).first().home_name == profile.home_name


def test_save_home_profile_flush_is_rollbackable(pg_db: Session) -> None:
    get_or_create_home_profile(pg_db)
    pg_db.commit()

    payload = HomeProfileUpsert(
        home_name="new_name",
        family_tags=["has_pet"],
        focus_points=[],
        system_style="family_companion",
        style_preference_text="",
        assistant_name="bot",
        home_note="",
    )
    # ``begin_nested()`` is the PostgreSQL analogue of the SQLite
    # session.transaction semantics that the original test relied on:
    # the subsequent flush lives in a SAVEPOINT, and the explicit
    # ``rollback()`` reverts only that SAVEPOINT (preserving the
    # commit before it). A bare ``pg_db.rollback()`` would also
    # revert the prior commit because the fixture owns the outer
    # transaction.
    save_tx = pg_db.begin_nested()
    save_home_profile(pg_db, payload)
    save_tx.rollback()

    pg_db.expire_all()
    assert pg_db.query(HomeProfile).first().home_name == "\u6211\u7684\u5bb6\u5ead"


def test_create_member_flush_is_rollbackable(pg_db: Session) -> None:
    entity = create_member(
        pg_db,
        {
            "name": "test_member",
            "role_type": "child",
            "age_group": "child",
        },
    )
    assert entity.id is not None

    pg_db.rollback()
    assert pg_db.query(HomeEntityProfile).count() == 0


def test_create_pet_flush_is_rollbackable(pg_db: Session) -> None:
    entity = create_pet(
        pg_db,
        {
            "name": "test_pet",
            "role_type": "cat",
            "age_group": "adult",
        },
    )
    assert entity.id is not None

    pg_db.rollback()
    assert pg_db.query(HomeEntityProfile).count() == 0


def test_update_entity_flush_is_rollbackable(pg_db: Session) -> None:
    entity = create_member(
        pg_db,
        {
            "name": "original",
            "role_type": "child",
            "age_group": "child",
        },
    )
    pg_db.commit()

    update_tx = pg_db.begin_nested()
    updated = update_entity(pg_db, entity.id, HomeEntityUpdate(name="changed"))
    assert updated is not None
    assert updated.name == "changed"
    update_tx.rollback()

    pg_db.expire_all()
    refreshed = pg_db.query(HomeEntityProfile).filter(HomeEntityProfile.id == entity.id).first()
    assert refreshed.name == "original"


def test_disable_entity_flush_is_rollbackable(pg_db: Session) -> None:
    entity = create_member(
        pg_db,
        {
            "name": "test",
            "role_type": "child",
            "age_group": "child",
        },
    )
    pg_db.commit()

    disable_tx = pg_db.begin_nested()
    assert disable_entity(pg_db, entity.id) is True
    disable_tx.rollback()

    pg_db.expire_all()
    refreshed = pg_db.query(HomeEntityProfile).filter(HomeEntityProfile.id == entity.id).first()
    assert refreshed.is_enabled is True


def test_create_pending_task_log_flush_is_rollbackable(pg_db: Session) -> None:
    task_log, created = create_pending_task_log(
        pg_db,
        task_type="session_build",
        task_target_id=1,
        detail_json={"scan_mode": "hot"},
    )
    assert created is True
    assert task_log.id is not None

    pg_db.rollback()
    assert pg_db.query(TaskLog).count() == 0


def test_transaction_composition_rollback_reverts_all(pg_db: Session) -> None:
    get_or_create_home_profile(pg_db)
    create_member(
        pg_db,
        {
            "name": "member1",
            "role_type": "child",
            "age_group": "child",
        },
    )

    pg_db.rollback()
    assert pg_db.query(HomeProfile).count() == 0
    assert pg_db.query(HomeEntityProfile).count() == 0


def test_transaction_composition_commit_persists_all(pg_db: Session) -> None:
    get_or_create_home_profile(pg_db)
    create_member(
        pg_db,
        {
            "name": "member1",
            "role_type": "child",
            "age_group": "child",
        },
    )
    pg_db.commit()

    pg_db.expire_all()
    assert pg_db.query(HomeProfile).count() == 1
    assert pg_db.query(HomeEntityProfile).count() == 1
