from src.models.video_source import VideoSource
from src.services.pipeline_constants import SUPPORTED_SCAN_SOURCE_TYPES, ValidationStatus


def is_source_type_supported(source_type: str) -> bool:
    return source_type in SUPPORTED_SCAN_SOURCE_TYPES


def is_source_schedulable(source: VideoSource) -> tuple[bool, str | None]:
    if not source.enabled:
        return False, "source_disabled"
    if source.source_paused:
        return False, "source_paused"
    if source.last_validate_status != ValidationStatus.SUCCESS:
        return False, "source_not_validated"
    if not is_source_type_supported(source.source_type):
        return False, "source_type_not_supported"
    return True, None
