from datetime import datetime
from typing import Any

from fastapi import APIRouter
from sqlalchemy import or_

from src.api.deps import DB, CurrentUser
from src.models.video_source import VideoSource
from src.schemas.response import BaseResponse, PaginatedData, PaginatedResponse, PaginationDetails
from src.schemas.video_source import (
    VideoPathValidateRequest,
    VideoPathValidateResponse,
    VideoSourceCreate,
    VideoSourceResponse,
    VideoSourceStatusResponse,
    VideoSourceUpdate,
)
from src.services.pipeline_constants import ValidationStatus
from src.services.video_source_policy import is_source_type_supported
from src.services.video_source_status import (
    build_video_source_status,
    build_video_sources_status_map,
)
from src.services.video_source_task_guard import find_running_source_task_type
from src.services.video_source_validator import validate_video_source_path

router = APIRouter()


@router.get("", response_model=PaginatedResponse[VideoSourceResponse])
def get_video_sources(
    db: DB,
    current_user: CurrentUser,
    page: int = 1,
    page_size: int = 20,
    enabled: bool | None = None,
    source_type: str | None = None,
    keyword: str | None = None,
) -> Any:
    query = db.query(VideoSource)
    if enabled is not None:
        query = query.filter(VideoSource.enabled == enabled)
    if source_type:
        query = query.filter(VideoSource.source_type == source_type)
    if keyword:
        query = query.filter(
            or_(
                VideoSource.source_name.ilike(f"%{keyword}%"),
                VideoSource.camera_name.ilike(f"%{keyword}%"),
            )
        )

    total = query.count()
    sources = query.offset((page - 1) * page_size).limit(page_size).all()

    return PaginatedResponse(
        data=PaginatedData(
            list=[VideoSourceResponse.model_validate(s) for s in sources],
            pagination=PaginationDetails(page=page, page_size=page_size, total=total),
        )
    )


@router.get("/status/batch", response_model=BaseResponse[list[VideoSourceStatusResponse]])
def get_video_sources_status_batch(
    db: DB,
    current_user: CurrentUser,
    source_ids: str,
) -> Any:
    parsed_ids: list[int] = []
    for value in source_ids.split(","):
        raw = value.strip()
        if not raw:
            continue
        if not raw.isdigit():
            return BaseResponse(code=4001, message=f"Invalid source id: {raw}")
        parsed_ids.append(int(raw))

    if not parsed_ids:
        return BaseResponse(data=[])

    status_map = build_video_sources_status_map(db=db, source_ids=parsed_ids)
    payload = [
        VideoSourceStatusResponse.model_validate(status_map[source_id])
        for source_id in parsed_ids
        if source_id in status_map
    ]
    return BaseResponse(data=payload)


@router.post("", response_model=BaseResponse[VideoSourceResponse])
def create_video_source(db: DB, current_user: CurrentUser, data: VideoSourceCreate) -> Any:
    source = VideoSource(**data.model_dump())
    db.add(source)
    db.commit()
    db.refresh(source)
    return BaseResponse(data=VideoSourceResponse.model_validate(source))


@router.get("/{id}", response_model=BaseResponse[VideoSourceResponse])
def get_video_source(db: DB, current_user: CurrentUser, id: int) -> Any:
    source = db.query(VideoSource).filter(VideoSource.id == id).first()
    if not source:
        return BaseResponse(code=4002, message="Source not found")
    return BaseResponse(data=VideoSourceResponse.model_validate(source))


@router.get("/{id}/status", response_model=BaseResponse[VideoSourceStatusResponse])
def get_video_source_status(db: DB, current_user: CurrentUser, id: int) -> Any:
    source = db.query(VideoSource).filter(VideoSource.id == id).first()
    if not source:
        return BaseResponse(code=4002, message="Source not found")

    status_payload = build_video_source_status(db=db, source_id=id)
    return BaseResponse(data=VideoSourceStatusResponse.model_validate(status_payload))


@router.post("/{id}/pause", response_model=BaseResponse[dict])
def pause_video_source(db: DB, current_user: CurrentUser, id: int) -> Any:
    source = db.query(VideoSource).filter(VideoSource.id == id).first()
    if not source:
        return BaseResponse(code=4002, message="Source not found")

    source.source_paused = True
    source.paused_at = datetime.utcnow()
    db.commit()
    return BaseResponse(data={"source_id": id, "source_paused": True})


@router.post("/{id}/resume", response_model=BaseResponse[dict])
def resume_video_source(db: DB, current_user: CurrentUser, id: int) -> Any:
    source = db.query(VideoSource).filter(VideoSource.id == id).first()
    if not source:
        return BaseResponse(code=4002, message="Source not found")

    source.source_paused = False
    source.paused_at = None
    db.commit()
    return BaseResponse(data={"source_id": id, "source_paused": False})


@router.put("/{id}", response_model=BaseResponse[VideoSourceResponse])
def update_video_source(db: DB, current_user: CurrentUser, id: int, data: VideoSourceUpdate) -> Any:
    source = db.query(VideoSource).filter(VideoSource.id == id).first()
    if not source:
        return BaseResponse(code=4002, message="Source not found")

    updates = data.model_dump(exclude_unset=True)
    old_source_type = source.source_type
    old_config_json = (
        source.config_json if isinstance(source.config_json, dict) else source.config_json
    )

    for key, value in updates.items():
        setattr(source, key, value)

    new_source_type = source.source_type
    new_config_json = (
        source.config_json if isinstance(source.config_json, dict) else source.config_json
    )
    if old_source_type != new_source_type or old_config_json != new_config_json:
        source.last_validate_status = None
        source.last_validate_message = None
        source.last_validate_at = None

    db.commit()
    db.refresh(source)
    return BaseResponse(data=VideoSourceResponse.model_validate(source))


@router.delete("/{id}", response_model=BaseResponse[dict])
def delete_video_source(db: DB, current_user: CurrentUser, id: int) -> Any:
    source = db.query(VideoSource).filter(VideoSource.id == id).first()
    if not source:
        return BaseResponse(code=4002, message="Source not found")
    running_task_type = find_running_source_task_type(db, id)
    if running_task_type is not None:
        return BaseResponse(
            code=4004,
            message=f"Source has running task: {running_task_type}",
        )

    db.delete(source)
    db.commit()
    return BaseResponse(data={})


@router.post("/{id}/enable", response_model=BaseResponse[dict])
def enable_video_source(db: DB, current_user: CurrentUser, id: int) -> Any:
    source = db.query(VideoSource).filter(VideoSource.id == id).first()
    if source:
        source.enabled = True
        db.commit()
    return BaseResponse(data={})


@router.post("/{id}/disable", response_model=BaseResponse[dict])
def disable_video_source(db: DB, current_user: CurrentUser, id: int) -> Any:
    source = db.query(VideoSource).filter(VideoSource.id == id).first()
    if source:
        source.enabled = False
        db.commit()
    return BaseResponse(data={})


@router.post("/{id}/test", response_model=BaseResponse[dict])
def test_video_source(db: DB, current_user: CurrentUser, id: int) -> Any:
    source = db.query(VideoSource).filter(VideoSource.id == id).first()
    if not source:
        return BaseResponse(code=4002, message="Source not found")

    config = source.config_json if isinstance(source.config_json, dict) else {}
    root_path = str(config.get("root_path") or "")
    result = validate_video_source_path(root_path)

    result_success = result.valid
    source.last_validate_status = (
        ValidationStatus.SUCCESS if result.valid else ValidationStatus.FAILED
    )
    if result.valid and not is_source_type_supported(source.source_type):
        result_success = False
        source.last_validate_status = ValidationStatus.FAILED
        source.last_validate_message = f"unsupported source type: {source.source_type}"
    else:
        source.last_validate_message = result.message
    source.last_validate_at = datetime.utcnow()
    db.commit()
    db.refresh(source)

    return BaseResponse(
        data={
            "success": result_success,
            "message": source.last_validate_message,
            "file_count": result.file_count,
            "latest_file_time": result.latest_file_time,
            "earliest_file_time": result.earliest_file_time,
            "last_validate_status": source.last_validate_status,
            "last_validate_message": source.last_validate_message,
            "last_validate_at": source.last_validate_at,
        }
    )


@router.post("/validate-path", response_model=BaseResponse[VideoPathValidateResponse])
def validate_path(db: DB, current_user: CurrentUser, payload: VideoPathValidateRequest) -> Any:
    result = validate_video_source_path(payload.path)
    return BaseResponse(
        data=VideoPathValidateResponse(
            valid=result.valid,
            file_count=result.file_count,
            latest_file_time=result.latest_file_time,
            earliest_file_time=result.earliest_file_time,
            message=result.message,
        )
    )
