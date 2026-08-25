import json
from collections.abc import Awaitable, Callable
from typing import Final

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

_STATUS_BY_CODE: Final[dict[int, int]] = {
    4000: 400,
    4001: 409,
    4002: 404,
    4004: 409,
    4011: 401,
    4290: 429,
    4291: 429,
    5000: 500,
    5001: 502,
    5002: 503,
}


def status_for_response_code(code: int) -> int | None:
    return _STATUS_BY_CODE.get(code)


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

        body = b"".join([chunk async for chunk in response.body_iterator])
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
