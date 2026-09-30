"""Auth HTTP contract tests: password change."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import src.api.deps as api_deps
from src.api.error_status import ResponseStatusMiddleware
from src.api.v1.endpoints import auth
from src.core.security import get_password_hash, verify_password
from src.models.admin_user import AdminUser


@pytest.fixture
def admin(pg_db: Session) -> AdminUser:
    user = AdminUser(username="admin", password_hash=get_password_hash("oldpassword123"))
    pg_db.add(user)
    pg_db.flush()
    return user


@pytest.fixture
def client(pg_db: Session, make_http_client, admin: AdminUser) -> Iterator[TestClient]:
    with make_http_client(
        [(auth.router, "/api/v1/auth")],
        middleware=[ResponseStatusMiddleware],
        extra_overrides={api_deps.get_current_user: lambda: admin},
    ) as test_client:
        yield test_client


def test_change_password_updates_hash(client: TestClient, admin: AdminUser) -> None:
    response = client.post(
        "/api/v1/auth/change-password",
        json={"old_password": "oldpassword123", "new_password": "newpassword123"},
    )

    assert response.status_code == 200
    assert response.json()["code"] == 0
    assert verify_password("newpassword123", admin.password_hash)
    assert not verify_password("oldpassword123", admin.password_hash)


def test_change_password_rejects_wrong_old_password(client: TestClient) -> None:
    response = client.post(
        "/api/v1/auth/change-password",
        json={"old_password": "wrong-password", "new_password": "newpassword123"},
    )

    assert response.status_code == 400
    assert response.json()["code"] == 4000


def test_change_password_rejects_short_new_password(client: TestClient) -> None:
    response = client.post(
        "/api/v1/auth/change-password",
        json={"old_password": "oldpassword123", "new_password": "short"},
    )

    assert response.status_code == 422
