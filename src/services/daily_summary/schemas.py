from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator


class SubjectSection(BaseModel):
    subject_name: str = Field(min_length=1)
    subject_type: Literal["member", "pet"]
    summary: str = Field(min_length=1)
    attention_needed: bool = False


class AttentionItem(BaseModel):
    title: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    level: str = Field(min_length=1)


class DailySummaryPromptResult(BaseModel):
    overall_summary: str = Field(min_length=1)
    subject_sections: list[SubjectSection] = Field(default_factory=list)
    attention_items: list[AttentionItem] = Field(default_factory=list)

    @field_validator("attention_items")
    @classmethod
    def validate_attention_items_count(cls, value: list[AttentionItem]) -> list[AttentionItem]:
        if len(value) > 3:
            raise ValueError("attention_items must not exceed 3 items")
        return value


class SubjectEventSummary(BaseModel):
    event_id: int
    event_type: Optional[str] = None
    title: str
    summary: str
    importance_level: Optional[str] = None
    recognition_status: str


class SubjectEventSection(BaseModel):
    subject_name: str
    subject_type: Literal["member", "pet"]
    related_event_count: int
    related_event_summaries: list[SubjectEventSummary] = Field(default_factory=list)


class AttentionCandidate(BaseModel):
    event_id: int
    event_type: Optional[str] = None
    title: str
    summary: str
    importance_level: Optional[str] = None
