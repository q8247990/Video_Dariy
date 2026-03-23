from typing import Any

from fastapi import APIRouter

from src.api.deps import DB, CurrentUser
from src.models.tag_definition import TagDefinition
from src.schemas.response import BaseResponse, PaginatedData, PaginatedResponse, PaginationDetails
from src.schemas.tag import TagCreate, TagResponse, TagUpdate

router = APIRouter()


@router.get("", response_model=PaginatedResponse[TagResponse])
def get_tags(
    db: DB,
    current_user: CurrentUser,
    page: int = 1,
    page_size: int = 20,
    tag_type: str | None = None,
    enabled: bool | None = None,
) -> Any:
    query = db.query(TagDefinition)
    if tag_type:
        query = query.filter(TagDefinition.tag_type == tag_type)
    if enabled is not None:
        query = query.filter(TagDefinition.enabled == enabled)

    query = query.order_by(TagDefinition.id.desc())

    total = query.count()
    tags = query.offset((page - 1) * page_size).limit(page_size).all()

    return PaginatedResponse(
        data=PaginatedData(
            list=[TagResponse.model_validate(t) for t in tags],
            pagination=PaginationDetails(page=page, page_size=page_size, total=total),
        )
    )


@router.post("", response_model=BaseResponse[TagResponse])
def create_tag(db: DB, current_user: CurrentUser, data: TagCreate) -> Any:
    tag = TagDefinition(**data.model_dump())
    db.add(tag)
    db.commit()
    db.refresh(tag)
    return BaseResponse(data=TagResponse.model_validate(tag))


@router.put("/{id}", response_model=BaseResponse[TagResponse])
def update_tag(db: DB, current_user: CurrentUser, id: int, data: TagUpdate) -> Any:
    tag = db.query(TagDefinition).filter(TagDefinition.id == id).first()
    if not tag:
        return BaseResponse(code=4002, message="Tag not found")

    for key, value in data.model_dump(exclude_unset=True).items():
        setattr(tag, key, value)

    db.commit()
    db.refresh(tag)
    return BaseResponse(data=TagResponse.model_validate(tag))
