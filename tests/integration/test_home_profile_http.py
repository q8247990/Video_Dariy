"""tests/integration/test_home_profile_http.py

家庭画像（Home Profile）端点 HTTP 契约测试。

将原 ``tests/unit/test_home_profile_endpoint.py`` 中"直接调用路由函数"的
用例改造为基于 :class:`fastapi.testclient.TestClient` 的真实 HTTP 契约
测试，断言：

* HTTP 状态码（``200`` 成功 / ``401`` 未鉴权 / ``404`` 不存在 /
  ``409`` 业务冲突 / ``422`` Pydantic 校验失败 / ``400`` 业务参数错误）
* JSON 响应体中的业务码 ``r.json()["code"]`` 与 ``data`` 字段
* DB 可观察状态（实体创建 / 软删除 / 图片写入与清理）

业务码 → HTTP 状态码的映射通过 :class:`ResponseStatusMiddleware`
在 ``make_http_client`` 工厂中启用：

    4001 / 4004 → 409、4002 → 404、4000 → 400

Pydantic 校验失败仍走 FastAPI 默认路径，返回 ``422`` 与 ``{"detail": [...]}``
标准错误体（端点模块无自定义覆盖）。
"""

from __future__ import annotations

import io
import os
from collections.abc import Generator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy.orm import Session

from src.api.error_status import ResponseStatusMiddleware
from src.api.v1.endpoints import home_profile
from src.core.config import settings
from src.models.home_entity_profile import HomeEntityProfile

# ---------------------------------------------------------------------------
# 共享夹具
# ---------------------------------------------------------------------------


@pytest.fixture
def client(pg_db: Session, make_http_client) -> Generator[TestClient, None, None]:
    """挂载 ``home_profile`` 路由并启用业务码 → HTTP 状态码中间件。"""

    with make_http_client(
        [(home_profile.router, "/api/v1/home-profile")],
        middleware=[ResponseStatusMiddleware],
    ) as test_client:
        yield test_client


@pytest.fixture
def entity_image_root(monkeypatch: pytest.MonkeyPatch, tmp_path) -> str:
    """把实体图片落盘目录重定向到测试临时目录，隔离文件系统副作用。"""

    root = str(tmp_path / "entity_images")
    monkeypatch.setattr(settings, "ENTITY_IMAGE_ROOT", root)
    return root


# ---------------------------------------------------------------------------
# 测试辅助
# ---------------------------------------------------------------------------


def _create_member(
    client: TestClient,
    *,
    name: str = "小米",
    role_type: str = "child",
    age_group: str = "child",
    sort_order: int = 1,
) -> dict[str, Any]:
    """通过 HTTP 创建一个 member 实体，返回响应 ``data`` 字典。"""

    response = client.post(
        "/api/v1/home-profile/entities/member",
        json={
            "name": name,
            "role_type": role_type,
            "age_group": age_group,
            "appearance_desc": "短发",
            "note": "客厅活动",
            "sort_order": sort_order,
            "is_enabled": True,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["code"] == 0, body
    return body["data"]


def _create_pet(
    client: TestClient,
    *,
    name: str = "布丁",
    role_type: str = "cat",
    breed: str = "橘猫",
    sort_order: int = 2,
) -> dict[str, Any]:
    """通过 HTTP 创建一个 pet 实体，返回响应 ``data`` 字典。"""

    response = client.post(
        "/api/v1/home-profile/entities/pet",
        json={
            "name": name,
            "role_type": role_type,
            "breed": breed,
            "personality_desc": "平时安静",
            "sort_order": sort_order,
            "is_enabled": True,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["code"] == 0, body
    return body["data"]


def _make_jpeg_bytes(width: int = 32, height: int = 32) -> bytes:
    """生成一张最小尺寸 JPEG 字节流，供图片上传测试使用。"""

    image = Image.new("RGB", (width, height), color=(120, 160, 200))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=85)
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# 鉴权契约
# ---------------------------------------------------------------------------


def test_request_without_token_returns_401(pg_db: Session, make_http_client) -> None:
    """未携带 Bearer token 时，受保护端点应统一返回 ``401``。"""

    with make_http_client(
        [(home_profile.router, "/api/v1/home-profile")],
        authenticated=False,
        middleware=[ResponseStatusMiddleware],
    ) as test_client:
        response = test_client.get("/api/v1/home-profile")

    assert response.status_code == 401


# ---------------------------------------------------------------------------
# 端点契约：GET / PUT /api/v1/home-profile
# ---------------------------------------------------------------------------


def test_get_home_profile_creates_default_record(client: TestClient) -> None:
    """首次 ``GET`` 应当懒创建默认家庭画像，并返回默认字段值。"""

    response = client.get("/api/v1/home-profile")

    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 0
    assert body["data"] is not None
    assert body["data"]["home_name"] == "我的家庭"
    assert body["data"]["assistant_name"] == "家庭助手"
    assert body["data"]["system_style"] == "family_companion"


def test_put_home_profile_updates_fields(client: TestClient, pg_db: Session) -> None:
    """``PUT`` 应当把校验过的字段写回 DB，并能在随后 ``GET`` 中读到。"""

    update_response = client.put(
        "/api/v1/home-profile",
        json={
            "home_name": "小王一家",
            "family_tags": ["has_pet"],
            "focus_points": ["pet_status"],
            "system_style": "family_companion",
            "style_preference_text": "描述简洁一点",
            "assistant_name": "小布",
            "home_note": "白天较安静",
        },
    )

    assert update_response.status_code == 200
    body = update_response.json()
    assert body["code"] == 0
    assert body["data"]["home_name"] == "小王一家"
    assert body["data"]["assistant_name"] == "小布"

    fetched = client.get("/api/v1/home-profile").json()["data"]
    assert fetched["home_name"] == "小王一家"
    assert fetched["family_tags"] == ["has_pet"]
    assert [item["key"] for item in fetched["focus_points"]] == ["pet_status"]
    assert fetched["focus_points"][0]["label"] == "宠物状态"

    assert pg_db.query(HomeEntityProfile).count() == 0


def test_put_home_profile_validation_422(client: TestClient) -> None:
    """``system_style`` 取值不在白名单时，Pydantic 校验失败应返回 ``422``。"""

    response = client.put(
        "/api/v1/home-profile",
        json={"system_style": "not_in_whitelist"},
    )

    assert response.status_code == 422
    assert "detail" in response.json()


# ---------------------------------------------------------------------------
# 端点契约：GET /api/v1/home-profile/entities
# ---------------------------------------------------------------------------


def test_list_entities_filters_by_type_and_includes_only_enabled(
    client: TestClient,
) -> None:
    """列表端点应支持 ``entity_type`` 过滤，并默认隐藏 ``is_enabled=False``。"""

    member = _create_member(client, name="小米")
    _create_pet(client, name="布丁")

    members_only = client.get(
        "/api/v1/home-profile/entities",
        params={"entity_type": "member"},
    )
    assert members_only.status_code == 200
    body = members_only.json()
    assert body["code"] == 0
    assert len(body["data"]) == 1
    assert body["data"][0]["name"] == "小米"
    assert body["data"][0]["entity_type"] == "member"

    pets_only = client.get(
        "/api/v1/home-profile/entities",
        params={"entity_type": "pet", "include_disabled": "true"},
    )
    assert pets_only.status_code == 200
    assert len(pets_only.json()["data"]) == 1

    delete_response = client.delete(f"/api/v1/home-profile/entities/{member['id']}")
    assert delete_response.status_code == 200

    default_after_disable = client.get(
        "/api/v1/home-profile/entities",
        params={"entity_type": "member"},
    )
    assert default_after_disable.status_code == 200
    assert default_after_disable.json()["data"] == []

    include_disabled = client.get(
        "/api/v1/home-profile/entities",
        params={"entity_type": "member", "include_disabled": "true"},
    )
    assert include_disabled.status_code == 200
    assert len(include_disabled.json()["data"]) == 1


def test_list_entities_invalid_type_returns_4000(client: TestClient) -> None:
    """``entity_type`` 取值不在 ``{member,pet}`` 时返回业务码 4000 → HTTP 400。"""

    response = client.get(
        "/api/v1/home-profile/entities",
        params={"entity_type": "robot"},
    )

    assert response.status_code == 400
    assert response.json()["code"] == 4000


# ---------------------------------------------------------------------------
# 端点契约：POST /api/v1/home-profile/entities/member 与 /entities/pet
# ---------------------------------------------------------------------------


def test_create_member_succeeds_and_persists(client: TestClient, pg_db: Session) -> None:
    """成功路径：HTTP 200 + code 0 + 行写入 DB。"""

    response = client.post(
        "/api/v1/home-profile/entities/member",
        json={
            "name": "小米",
            "role_type": "child",
            "age_group": "child",
            "appearance_desc": "短发",
            "sort_order": 1,
            "is_enabled": True,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 0
    assert body["data"]["name"] == "小米"
    assert body["data"]["entity_type"] == "member"

    persisted = (
        pg_db.query(HomeEntityProfile).filter(HomeEntityProfile.id == body["data"]["id"]).one()
    )
    assert persisted.is_enabled is True


def test_create_member_invalid_role_returns_422(client: TestClient) -> None:
    """``role_type`` 不在 member 白名单时，Pydantic 校验失败 → ``422``。"""

    response = client.post(
        "/api/v1/home-profile/entities/member",
        json={"name": "小米", "role_type": "not_a_role"},
    )

    assert response.status_code == 422
    assert "detail" in response.json()


def test_create_pet_succeeds_and_persists(client: TestClient, pg_db: Session) -> None:
    """成功路径：pet 实体写入 DB，``entity_type`` 固定为 ``pet``。"""

    response = client.post(
        "/api/v1/home-profile/entities/pet",
        json={
            "name": "布丁",
            "role_type": "cat",
            "breed": "橘猫",
            "personality_desc": "平时安静",
            "sort_order": 2,
            "is_enabled": True,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 0
    assert body["data"]["entity_type"] == "pet"
    assert body["data"]["breed"] == "橘猫"

    persisted = (
        pg_db.query(HomeEntityProfile).filter(HomeEntityProfile.id == body["data"]["id"]).one()
    )
    assert persisted.entity_type == "pet"


def test_create_pet_invalid_role_returns_422(client: TestClient) -> None:
    """``role_type`` 不在 pet 白名单时，Pydantic 校验失败 → ``422``。"""

    response = client.post(
        "/api/v1/home-profile/entities/pet",
        json={"name": "布丁", "role_type": "not_a_pet"},
    )

    assert response.status_code == 422


# ---------------------------------------------------------------------------
# 端点契约：PUT / DELETE /api/v1/home-profile/entities/{entity_id}
# ---------------------------------------------------------------------------


def test_update_entity_success_and_persists(client: TestClient, pg_db: Session) -> None:
    """成功路径：部分字段更新生效，且能观察到 DB 状态变化。"""

    member = _create_member(client, name="小米")

    response = client.put(
        f"/api/v1/home-profile/entities/{member['id']}",
        json={"name": "小米米", "note": "更新备注"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 0
    assert body["data"]["name"] == "小米米"
    assert body["data"]["note"] == "更新备注"

    persisted = pg_db.query(HomeEntityProfile).filter(HomeEntityProfile.id == member["id"]).one()
    assert persisted.name == "小米米"
    assert persisted.note == "更新备注"


def test_update_entity_not_found_returns_404(client: TestClient) -> None:
    """不存在的 ``entity_id`` 应返回业务码 4002 → HTTP 404。"""

    response = client.put(
        "/api/v1/home-profile/entities/99999",
        json={"name": "不存在"},
    )

    assert response.status_code == 404
    assert response.json()["code"] == 4002


def test_delete_entity_soft_disables_and_hides_from_default_list(
    client: TestClient, pg_db: Session
) -> None:
    """``DELETE`` 应当软删除（``is_enabled=False``），默认列表不再返回该实体。"""

    _create_member(client, name="小米")
    pet = _create_pet(client, name="布丁")

    delete_response = client.delete(f"/api/v1/home-profile/entities/{pet['id']}")
    assert delete_response.status_code == 200
    assert delete_response.json()["code"] == 0

    list_members = client.get(
        "/api/v1/home-profile/entities",
        params={"entity_type": "member"},
    )
    assert list_members.status_code == 200
    assert len(list_members.json()["data"]) == 1
    assert list_members.json()["data"][0]["name"] == "小米"

    list_pets = client.get(
        "/api/v1/home-profile/entities",
        params={"entity_type": "pet"},
    )
    assert list_pets.status_code == 200
    assert list_pets.json()["data"] == []

    persisted = pg_db.query(HomeEntityProfile).filter(HomeEntityProfile.id == pet["id"]).one()
    assert persisted.is_enabled is False


def test_delete_entity_not_found_returns_404(client: TestClient) -> None:
    """不存在的 ``entity_id`` 应返回业务码 4002 → HTTP 404。"""

    response = client.delete("/api/v1/home-profile/entities/99999")

    assert response.status_code == 404
    assert response.json()["code"] == 4002


# ---------------------------------------------------------------------------
# 端点契约：POST / GET / DELETE /api/v1/home-profile/entities/{entity_id}/image
# ---------------------------------------------------------------------------


def test_upload_image_writes_file_and_records_path(
    client: TestClient,
    pg_db: Session,
    entity_image_root: str,
) -> None:
    """成功路径：JPEG 上传后落盘到 ``ENTITY_IMAGE_ROOT``，DB ``image_path`` 被设置。"""

    member = _create_member(client, name="小米")
    payload = _make_jpeg_bytes()

    response = client.post(
        f"/api/v1/home-profile/entities/{member['id']}/image",
        files={"file": ("avatar.jpg", payload, "image/jpeg")},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 0
    assert body["data"]["id"] == member["id"]

    expected_path = f"{entity_image_root}/entity_{member['id']}.jpg"
    with open(expected_path, "rb") as handle:
        assert handle.read() == payload

    persisted = pg_db.query(HomeEntityProfile).filter(HomeEntityProfile.id == member["id"]).one()
    assert persisted.image_path == expected_path


def test_upload_image_not_found_returns_404(client: TestClient, entity_image_root: str) -> None:
    """不存在的 ``entity_id`` 应返回业务码 4002 → HTTP 404。"""

    payload = _make_jpeg_bytes()
    response = client.post(
        "/api/v1/home-profile/entities/99999/image",
        files={"file": ("avatar.jpg", payload, "image/jpeg")},
    )

    assert response.status_code == 404
    assert response.json()["code"] == 4002


def test_upload_image_unsupported_content_type_returns_4000(
    client: TestClient, entity_image_root: str
) -> None:
    """非白名单 ``content_type`` 应返回业务码 4000 → HTTP 400。"""

    member = _create_member(client, name="小米")
    payload = b"not really an image"

    response = client.post(
        f"/api/v1/home-profile/entities/{member['id']}/image",
        files={"file": ("avatar.txt", payload, "text/plain")},
    )

    assert response.status_code == 400
    assert response.json()["code"] == 4000


def test_get_image_without_token_returns_401(client: TestClient, entity_image_root: str) -> None:
    """无 media capability token 时，签名端点应返回 ``HTTP 401``。"""

    member = _create_member(client, name="小米")
    response = client.get(f"/api/v1/home-profile/entities/{member['id']}/image")

    assert response.status_code == 401
    assert response.json()["detail"] == "Missing media capability"


def test_get_image_with_signed_token_returns_jpeg(
    client: TestClient,
    pg_db: Session,
    entity_image_root: str,
) -> None:
    """使用合法签名 token 后应返回 ``image/jpeg`` 字节流。"""

    from src.services.media_signing import (
        MediaCapability,
        MediaSigningService,
    )

    member = _create_member(client, name="小米")
    payload = _make_jpeg_bytes()
    upload_response = client.post(
        f"/api/v1/home-profile/entities/{member['id']}/image",
        files={"file": ("avatar.jpg", payload, "image/jpeg")},
    )
    assert upload_response.status_code == 200

    capability = MediaCapability(
        resource_kind="image",
        resource_id=member["id"],
        method="GET",
        expires_at=2_000_000_000,
    )
    token = MediaSigningService.from_settings().issue(capability)

    response = client.get(
        f"/api/v1/home-profile/entities/{member['id']}/image",
        params={"token": token},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/jpeg")
    assert response.content == payload


def test_get_image_without_uploaded_file_returns_409(
    client: TestClient,
    pg_db: Session,
    entity_image_root: str,
) -> None:
    """实体存在但尚未上传图片时，应返回业务码 4004 → HTTP 409。"""

    from src.services.media_signing import (
        MediaCapability,
        MediaSigningService,
    )

    member = _create_member(client, name="小米")

    capability = MediaCapability(
        resource_kind="image",
        resource_id=member["id"],
        method="GET",
        expires_at=2_000_000_000,
    )
    token = MediaSigningService.from_settings().issue(capability)

    response = client.get(
        f"/api/v1/home-profile/entities/{member['id']}/image",
        params={"token": token},
    )

    assert response.status_code == 409
    assert response.json()["code"] == 4004


def test_delete_image_clears_path_and_removes_file(
    client: TestClient,
    pg_db: Session,
    entity_image_root: str,
) -> None:
    """成功路径：删除后 DB ``image_path`` 为 ``None``，落盘文件被清理。"""

    member = _create_member(client, name="小米")
    payload = _make_jpeg_bytes()
    upload_response = client.post(
        f"/api/v1/home-profile/entities/{member['id']}/image",
        files={"file": ("avatar.jpg", payload, "image/jpeg")},
    )
    assert upload_response.status_code == 200

    expected_path = f"{entity_image_root}/entity_{member['id']}.jpg"
    assert os.path.exists(expected_path)

    delete_response = client.delete(f"/api/v1/home-profile/entities/{member['id']}/image")
    assert delete_response.status_code == 200
    assert delete_response.json()["code"] == 0

    assert not os.path.exists(expected_path)

    persisted = pg_db.query(HomeEntityProfile).filter(HomeEntityProfile.id == member["id"]).one()
    assert persisted.image_path is None


def test_delete_image_not_found_returns_404(client: TestClient) -> None:
    """不存在的 ``entity_id`` 应返回业务码 4002 → HTTP 404。"""

    response = client.delete("/api/v1/home-profile/entities/99999/image")

    assert response.status_code == 404
    assert response.json()["code"] == 4002
