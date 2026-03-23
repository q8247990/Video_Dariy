from datetime import datetime
from typing import Any

from fastapi import APIRouter

from src.api.deps import DB, CurrentUser
from src.application.qa.schemas import QARequest
from src.application.qa.service import (
    QAProviderInvokeError,
    QAProviderNotConfiguredError,
    QAService,
)
from src.models.chat_query_log import ChatQueryLog
from src.schemas.chat import ChatAskRequest, ChatAskResponse, ChatQueryLogResponse
from src.schemas.response import BaseResponse, PaginatedData, PaginatedResponse, PaginationDetails

router = APIRouter()


@router.post("/ask", response_model=BaseResponse[ChatAskResponse])
def ask_question(db: DB, current_user: CurrentUser, request: ChatAskRequest) -> Any:
    try:
        service = QAService(db)
        result = service.answer(
            QARequest(
                question=request.question,
                now=datetime.now(),
                timezone="Asia/Shanghai",
                write_query_log=True,
                request_source="web",
            )
        )
    except QAProviderNotConfiguredError:
        return BaseResponse(code=5000, message="No QA provider configured")
    except QAProviderInvokeError as e:
        return BaseResponse(code=5002, message=str(e))
    except ValueError as e:
        return BaseResponse(code=4000, message=str(e))

    return BaseResponse(
        data=ChatAskResponse(
            question=result.question,
            answer_text=result.answer_text,
            referenced_events=[
                {"id": item.id, "description": item.summary or item.title}
                for item in result.referenced_events
            ],
            referenced_sessions=[{"id": item.id} for item in result.referenced_sessions],
        )
    )


@router.get("/history", response_model=PaginatedResponse[ChatQueryLogResponse])
def get_chat_history(db: DB, current_user: CurrentUser, page: int = 1, page_size: int = 20) -> Any:
    query = db.query(ChatQueryLog).order_by(ChatQueryLog.created_at.desc())

    total = query.count()
    logs = query.offset((page - 1) * page_size).limit(page_size).all()

    return PaginatedResponse(
        data=PaginatedData(
            list=[ChatQueryLogResponse.model_validate(log) for log in logs],
            pagination=PaginationDetails(page=page, page_size=page_size, total=total),
        )
    )
