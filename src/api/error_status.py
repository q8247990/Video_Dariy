import json
from collections.abc import Awaitable, Callable
from enum import IntEnum
from typing import Any, Final, cast

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response as StarletteResponse


class BusinessCode(IntEnum):
    """Central business error codes used across API boundaries.

    ``OK`` is the success sentinel; every other member is a stable
    business code that the API returns inside ``BaseResponse.code``.
    Values must never change — existing API clients and the
    HTTP-mapping below depend on them.
    """

    OK = 0
    VALIDATION_ERROR = 4000
    CONFLICT = 4001
    NOT_FOUND = 4002
    FORBIDDEN = 4003
    UNPROCESSABLE_ENTITY = 4004
    REFERENCE_EXISTS = 4005
    UNAUTHORIZED = 4011
    RATE_LIMITED = 4290
    QUOTA_EXCEEDED = 4291
    INTERNAL_ERROR = 5000
    UPSTREAM_ERROR = 5001
    UNAVAILABLE = 5002
    CONFIGURATION_ERROR = 5003


_HTTP_STATUS_BY_CODE: Final[dict[int, int]] = {
    int(BusinessCode.VALIDATION_ERROR): 400,
    int(BusinessCode.CONFLICT): 409,
    int(BusinessCode.NOT_FOUND): 404,
    int(BusinessCode.UNPROCESSABLE_ENTITY): 409,
    int(BusinessCode.UNAUTHORIZED): 401,
    int(BusinessCode.RATE_LIMITED): 429,
    int(BusinessCode.QUOTA_EXCEEDED): 429,
    int(BusinessCode.INTERNAL_ERROR): 500,
    int(BusinessCode.UPSTREAM_ERROR): 502,
    int(BusinessCode.UNAVAILABLE): 503,
}


def status_for_response_code(code: int) -> int | None:
    return _HTTP_STATUS_BY_CODE.get(code)


class ResponseStatusMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        response = await call_next(request)
        content_type = response.headers.get("content-type", "")
        if "application/json" not in content_type or response.status_code != 200:
            return response

        starlette_response = cast(StarletteResponse, response)
        body_iterator: Any = cast(Any, starlette_response).body_iterator
        body = b"".join([chunk async for chunk in body_iterator])
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return Response(
                content=body,
                status_code=response.status_code,
                headers=dict(response.headers),
                media_type=response.media_type,
            )

        if not isinstance(payload, dict) or not isinstance(payload.get("code"), int):
            return JSONResponse(
                content=payload,
                status_code=response.status_code,
                headers=dict(response.headers),
            )

        status_code = status_for_response_code(payload["code"])
        return JSONResponse(
            content=payload,
            status_code=status_code or response.status_code,
            headers=dict(response.headers),
        )
