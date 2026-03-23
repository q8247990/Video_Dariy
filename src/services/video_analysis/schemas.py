from typing import Optional

from pydantic import BaseModel, Field, field_validator

from src.services.video_analysis.enums import (
    ACTIVITY_LEVELS,
    ANALYSIS_NOTE_TYPES,
    IMPORTANCE_LEVELS,
    RECOGNITION_STATUSES,
    VIDEO_EVENT_TYPES,
)


class SessionSummaryDTO(BaseModel):
    summary_text: str = Field(min_length=1)
    activity_level: str
    main_subjects: list[str] = Field(default_factory=list)
    has_important_event: bool

    @field_validator("activity_level")
    @classmethod
    def validate_activity_level(cls, value: str) -> str:
        if value not in ACTIVITY_LEVELS:
            raise ValueError("invalid activity_level")
        return value


class RelatedEntityDTO(BaseModel):
    entity_type: str
    display_name: str
    matched_profile_name: Optional[str] = None
    recognition_status: str
    confidence: Optional[float] = Field(default=None, ge=0, le=1)

    @field_validator("recognition_status")
    @classmethod
    def validate_recognition_status(cls, value: str) -> str:
        if value not in RECOGNITION_STATUSES:
            raise ValueError("invalid recognition_status")
        return value


class RecognizedEventDTO(BaseModel):
    offset_start_sec: float = Field(ge=0)
    offset_end_sec: float = Field(ge=0)
    event_type: str
    title: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    detail: str = Field(min_length=1)
    related_entities: list[RelatedEntityDTO] = Field(default_factory=list)
    observed_actions: list[str] = Field(default_factory=list)
    interpreted_state: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
    importance_level: str

    @field_validator("event_type")
    @classmethod
    def validate_event_type(cls, value: str) -> str:
        if value not in VIDEO_EVENT_TYPES:
            raise ValueError("invalid event_type")
        return value

    @field_validator("importance_level")
    @classmethod
    def validate_importance_level(cls, value: str) -> str:
        if value not in IMPORTANCE_LEVELS:
            raise ValueError("invalid importance_level")
        return value

    @field_validator("offset_end_sec")
    @classmethod
    def validate_offsets(cls, value: float, info) -> float:
        offset_start_sec = info.data.get("offset_start_sec")
        if offset_start_sec is not None and value < offset_start_sec:
            raise ValueError("offset_end_sec must be greater than or equal to offset_start_sec")
        return value


class AnalysisNoteDTO(BaseModel):
    type: str
    note: str = Field(min_length=1)

    @field_validator("type")
    @classmethod
    def validate_note_type(cls, value: str) -> str:
        if value not in ANALYSIS_NOTE_TYPES:
            raise ValueError("invalid analysis_notes.type")
        return value


class RecognitionResultDTO(BaseModel):
    session_summary: SessionSummaryDTO
    events: list[RecognizedEventDTO] = Field(default_factory=list)
    analysis_notes: list[AnalysisNoteDTO] = Field(default_factory=list)
