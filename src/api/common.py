"""API 层公共工具函数。

提供 get_or_404、paginate、reset_default_provider 等重复模式的统一实现。
"""

from typing import Any, Type, TypeVar

from sqlalchemy.orm import Session

from src.models.llm_provider import LLMProvider
from src.schemas.response import PaginatedData, PaginatedResponse, PaginationDetails

T = TypeVar("T")


def get_or_404(db: Session, model: Type[T], record_id: int, message: str = "Not found") -> T | None:
    """查找记录，不存在时返回 None（调用方负责返回 BaseResponse(code=4002)）。

    用法::

        source = get_or_404(db, VideoSource, id, "Source not found")
        if source is None:
            return BaseResponse(code=4002, message="Source not found")
    """
    return db.query(model).filter(model.id == record_id).first()  # type: ignore[attr-defined]


def paginate(
    query: Any,
    page: int,
    page_size: int,
    schema: Type[Any],
    *,
    transform: Any = None,
) -> PaginatedResponse:
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


def reset_other_default_providers(db: Session, provider: LLMProvider) -> None:
    """将同类型其他 provider 的 default 标记清除，保证唯一默认。

    在 create_provider / update_provider 设置 is_default_vision 或
    is_default_qa 后调用。
    """
    if provider.is_default_vision:
        db.query(LLMProvider).filter(
            LLMProvider.supports_vision.is_(True),
            LLMProvider.id != provider.id,
        ).update({"is_default_vision": False})

    if provider.is_default_qa:
        db.query(LLMProvider).filter(
            LLMProvider.supports_qa.is_(True),
            LLMProvider.id != provider.id,
        ).update({"is_default_qa": False})
