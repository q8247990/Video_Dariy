from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.schemas.response import BaseResponse


def test_compatibility_errors_use_http_statuses_without_changing_payload() -> None:
    from src.api.error_status import ResponseStatusMiddleware

    app = FastAPI()
    app.add_middleware(ResponseStatusMiddleware)

    @app.get("/auth")
    def auth_error() -> BaseResponse[None]:
        return BaseResponse(code=4011, message="invalid credentials")

    @app.get("/missing")
    def missing_error() -> BaseResponse[None]:
        return BaseResponse(code=4002, message="not found")

    @app.get("/conflict")
    def conflict_error() -> BaseResponse[None]:
        return BaseResponse(code=4004, message="conflict")

    @app.get("/external")
    def external_error() -> BaseResponse[None]:
        return BaseResponse(code=5001, message="provider unavailable")

    with TestClient(app) as client:
        auth_response = client.get("/auth")
        missing_response = client.get("/missing")
        conflict_response = client.get("/conflict")
        external_response = client.get("/external")

    assert auth_response.status_code == 401
    assert missing_response.status_code == 404
    assert conflict_response.status_code == 409
    assert external_response.status_code == 502
    assert auth_response.json() == {"code": 4011, "message": "invalid credentials", "data": None}
    assert missing_response.json()["code"] == 4002
    assert conflict_response.json()["code"] == 4004
    assert external_response.json()["code"] == 5001


def test_validation_error_uses_422_with_compatibility_payload() -> None:
    from src.main import app

    client = TestClient(app)
    response = client.post("/api/v1/auth/login", json={})

    assert response.status_code == 422
    assert response.json()["code"] == 4000
    assert response.json()["data"] is None
