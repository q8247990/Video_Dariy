from typing import Generic, Optional, TypeVar

from pydantic import BaseModel

T = TypeVar("T")


class BaseResponse(BaseModel, Generic[T]):
    code: int = 0
    message: str = "ok"
    data: Optional[T] = None


class PaginationDetails(BaseModel):
    page: int
    page_size: int
    total: int


class PaginatedData(BaseModel, Generic[T]):
    list: list[T]
    pagination: PaginationDetails


class PaginatedResponse(BaseResponse[PaginatedData[T]]):
    pass
