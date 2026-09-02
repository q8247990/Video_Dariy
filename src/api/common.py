"""API 层公共工具函数。

提供统一的 GET 列表分页辅助，消除各端点手写 count/offset/limit 的重复。
"""

from typing import Any, Callable, Optional, TypeVar

from pydantic import BaseModel

from src.schemas.response import PaginatedData, PaginatedResponse, PaginationDetails

T = TypeVar("T", bound=BaseModel)


def paginate(
    query: Any,
    page: int,
    page_size: int,
    schema: type[T],
    *,
    transform: Optional[Callable[[Any], Any]] = None,
) -> PaginatedResponse[T]:
    """对 SQLAlchemy query 执行分页并返回 PaginatedResponse。

    Args:
        query: SQLAlchemy query 对象（已应用过滤/排序）
        page: 页码（从 1 开始）
        page_size: 每页条数
        schema: Pydantic schema 类，用于 model_validate
        transform: 可选的转换函数 (row) -> dict，用于 join 查询等复杂场景
    """
    total = query.count()
    rows = query.offset((page - 1) * page_size).limit(page_size).all()

    if transform is not None:
        items = [schema.model_validate(transform(row)) for row in rows]
    else:
        items = [schema.model_validate(row) for row in rows]

    return PaginatedResponse(
        data=PaginatedData(
            list=items,
            pagination=PaginationDetails(page=page, page_size=page_size, total=total),
        )
    )
