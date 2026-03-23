from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    PROJECT_NAME: str = "Home Monitor Video Analysis"
    API_V1_STR: str = "/api/v1"

    # Security
    SECRET_KEY: str = "supersecretkey_please_change_in_production"
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
    SESSION_PLAYBACK_MODE: str = "hls_index_only"

    # Analysis
    ANALYZER_SEGMENT_SECONDS: int = 600

    # DB bootstrap
    DB_INIT_MAX_RETRIES: int = 120
    DB_INIT_RETRY_INTERVAL_SECONDS: int = 2

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


settings = Settings()
