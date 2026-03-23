class SourceType:
    LOCAL_DIRECTORY = "local_directory"


SUPPORTED_SCAN_SOURCE_TYPES = {SourceType.LOCAL_DIRECTORY}


class ValidationStatus:
    SUCCESS = "success"
    FAILED = "failed"


class TaskType:
    SESSION_BUILD = "session_build"
    SESSION_ANALYSIS = "session_analysis"
    VIDEO_PIPELINE_ALERT = "video_pipeline_alert"
    DAILY_SUMMARY_GENERATION = "daily_summary_generation"
    WEBHOOK_PUSH = "webhook_push"


class ScanMode:
    HOT = "hot"
    FULL = "full"


class TaskStatus:
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    SKIPPED = "skipped"
    FAILED = "failed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"


class SessionAnalysisStatus:
    OPEN = "open"
    SEALED = "sealed"
    ANALYZING = "analyzing"
    SUCCESS = "success"
    FAILED = "failed"


class AnalysisPriority:
    HOT = "hot"
    FULL = "full"


class PipelineStatus:
    IDLE = "idle"
    BUILDING = "building"
    ANALYZING = "analyzing"
    FAILED = "failed"
