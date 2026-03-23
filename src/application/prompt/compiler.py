from src.application.prompt.contracts import (
    DailySummaryPromptInput,
    QAAnswerPromptInput,
    QAIntentPromptInput,
    VideoRecognitionPromptInput,
)
from src.services.prompt_builder.daily_summary import build_daily_summary_prompt
from src.services.prompt_builder.qa_answer import build_qa_answer_prompt
from src.services.prompt_builder.qa_intent import build_qa_intent_prompt
from src.services.prompt_builder.video_recognition import build_video_recognition_prompt


def compile_video_recognition_prompt(input_data: VideoRecognitionPromptInput) -> str:
    return build_video_recognition_prompt(
        {
            "home_context": input_data.home_context,
            "video_source": {
                "source_name": input_data.video_source.source_name,
                "camera_name": input_data.video_source.camera_name,
                "location_name": input_data.video_source.location_name,
                "prompt_text": input_data.video_source.prompt_text,
                "source_type": input_data.video_source.source_type,
            },
            "session_context": {
                "session_id": input_data.session_context.session_id,
                "source_id": input_data.session_context.source_id,
                "session_start_time": input_data.session_context.session_start_time,
                "session_end_time": input_data.session_context.session_end_time,
                "total_duration_seconds": input_data.session_context.total_duration_seconds,
                "segment_index": input_data.session_context.segment_index,
                "segment_start_offset_sec": input_data.session_context.segment_start_offset_sec,
                "segment_duration_seconds": input_data.session_context.segment_duration_seconds,
            },
            "strategy_context": {
                "ingest_type": input_data.strategy_context.ingest_type,
                "source_type": input_data.strategy_context.source_type,
                "strategy_note": input_data.strategy_context.strategy_note,
            },
            "event_type_list": input_data.event_type_list,
        }
    )


def compile_daily_summary_prompt(input_data: DailySummaryPromptInput) -> str:
    return build_daily_summary_prompt(
        {
            "home_context": input_data.home_context,
            "summary_date": input_data.summary_date,
            "time_range": {
                "start": input_data.time_range_start,
                "end": input_data.time_range_end,
            },
            "subject_sections": input_data.subject_sections,
            "missing_subjects": input_data.missing_subjects,
            "attention_candidates": input_data.attention_candidates,
        }
    )


def compile_qa_intent_prompt(input_data: QAIntentPromptInput) -> tuple[str, str]:
    return build_qa_intent_prompt(
        question=input_data.question,
        now=input_data.now,
        timezone=input_data.timezone,
        home_context=input_data.home_context,
    )


def compile_qa_answer_prompt(input_data: QAAnswerPromptInput) -> tuple[str, str]:
    return build_qa_answer_prompt(
        question=input_data.question,
        now_iso=input_data.now_iso,
        timezone=input_data.timezone,
        home_context_text=input_data.home_context_text,
        query_plan=input_data.query_plan,
        evidence=input_data.evidence,
    )
