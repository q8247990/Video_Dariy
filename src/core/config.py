from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class SecretConfigurationError(ValueError):
    """Raised when a production deployment has an unsafe secret configuration."""


_DEVELOPMENT_SECRET_KEY = "supersecretkey_please_change_in_production"
_DEVELOPMENT_MEDIA_SIGNING_KEY = "development-media-signing-key-change-before-production"
_DEVELOPMENT_PROVIDER_KEY_ENCRYPTION_KEY = "v1:QOWWQiHxXhQxyTXkaTPzU5Xx-wYc5yqD1kuWNpCEJDg="
_KNOWN_INSECURE_SECRETS = frozenset(
    {
        "",
        _DEVELOPMENT_SECRET_KEY,
        _DEVELOPMENT_MEDIA_SIGNING_KEY,
        _DEVELOPMENT_PROVIDER_KEY_ENCRYPTION_KEY,
        "change_me_mcp_token",
        "123456",
        "super-secret-key",
    }
)


class Settings(BaseSettings):
    PROJECT_NAME: str = "Home Monitor Video Analysis"
    API_V1_STR: str = "/api/v1"
    DEFAULT_LOCALE: str = "zh-CN"

    # Security
    APP_ENV: str = "development"
    SECRET_KEY: str = _DEVELOPMENT_SECRET_KEY
    MEDIA_SIGNING_KEY: str = _DEVELOPMENT_MEDIA_SIGNING_KEY
    PROVIDER_KEY_ENCRYPTION_KEY: str = _DEVELOPMENT_PROVIDER_KEY_ENCRYPTION_KEY
    MCP_TOKEN: str = "change_me_mcp_token"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24 * 7  # 7 days

    # Bootstrap admin
    DEFAULT_ADMIN_USERNAME: str = "admin"
    DEFAULT_ADMIN_PASSWORD: str = "123456"

    # PostgreSQL Database
    DATABASE_URL: str = "postgresql+psycopg://postgres:123456@localhost:5432/home_monitor"

    @property
    def SQLALCHEMY_DATABASE_URI(self) -> str:
        return self.DATABASE_URL

    # Redis & Celery
    REDIS_URL: str = "redis://localhost:6379/0"

    # Storage
    VIDEO_ROOT_PATH: str = "/data/videos"
    PLAYBACK_CACHE_ROOT: str = "/data/hls"
    ENTITY_IMAGE_ROOT: str = "/data/images"
    SESSION_PLAYBACK_MODE: str = "hls_index_only"

    # Analysis
    ANALYZER_SEGMENT_SECONDS: int = 600
    ANALYZER_LLM_CHUNK_SECONDS: int = 60
    ANALYZER_VIDEO_KEYFRAME_PERIOD_SECONDS: int = 8
    ANALYZER_VIDEO_KEYFRAME_MAD_THRESHOLD: float = 1.0
    ANALYZER_VIDEO_KEYFRAME_PHASH_THRESHOLD: int = 6
    ANALYZER_VIDEO_KEYFRAME_FALLBACK_TO_MP4: bool = True
    ANALYSIS_LEASE_SECONDS: int = 300
    ANALYSIS_PROGRESS_GRACE_SECONDS: int = 600
    ANALYSIS_PENDING_GRACE_SECONDS: int = 1800
    ANALYSIS_RECOVERY_MAX_ATTEMPTS: int = 3
    PENDING_UNLEASSED_TIMEOUT_SECONDS: int = 300

    # DB bootstrap
    DB_INIT_MAX_RETRIES: int = 120
    DB_INIT_RETRY_INTERVAL_SECONDS: int = 2

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @model_validator(mode="after")
    def _validate_llm_chunk_seconds(self) -> "Settings":
        if self.ANALYZER_LLM_CHUNK_SECONDS <= 0:
            raise ValueError("ANALYZER_LLM_CHUNK_SECONDS must be > 0")
        if self.ANALYZER_LLM_CHUNK_SECONDS > self.ANALYZER_SEGMENT_SECONDS:
            raise ValueError(
                f"ANALYZER_LLM_CHUNK_SECONDS ({self.ANALYZER_LLM_CHUNK_SECONDS}) "
                f"must be <= ANALYZER_SEGMENT_SECONDS "
                f"({self.ANALYZER_SEGMENT_SECONDS})"
            )
        if self.APP_ENV.strip().lower() == "production":
            self._validate_production_secrets()
        return self

    def _validate_production_secrets(self) -> None:
        """Reject missing, known-default, or shared production signing material."""
        invalid_names = [
            name
            for name, value in (
                ("SECRET_KEY", self.SECRET_KEY),
                ("MEDIA_SIGNING_KEY", self.MEDIA_SIGNING_KEY),
                ("PROVIDER_KEY_ENCRYPTION_KEY", self.PROVIDER_KEY_ENCRYPTION_KEY),
            )
            if value.strip() in _KNOWN_INSECURE_SECRETS
        ]
        if invalid_names:
            raise SecretConfigurationError(
                "Production secret configuration is missing or uses an unsafe default: "
                + ", ".join(invalid_names)
            )
        if self.SECRET_KEY == self.MEDIA_SIGNING_KEY:
            raise SecretConfigurationError(
                "Production SECRET_KEY and MEDIA_SIGNING_KEY must be distinct"
            )


settings = Settings()
