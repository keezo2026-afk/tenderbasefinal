"""Strongly typed application configuration.

Every value is sourced from the environment (or a local ``.env`` file).
Nothing secret is ever hard-coded. AI credentials are strictly optional: the
core API must start and serve traffic without any AI provider configured.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

AppEnv = Literal["development", "staging", "production", "test"]


class Settings(BaseSettings):
    """Runtime configuration for the TenderBase API and its workers."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # -- Application ------------------------------------------------------
    app_env: AppEnv = "development"
    app_name: str = "TenderBase API"
    app_version: str = "0.1.0"
    debug: bool = False

    # -- API --------------------------------------------------------------
    api_prefix: str = "/api"
    log_level: str = "INFO"
    log_json: bool = False
    cors_origins: list[str] = Field(default_factory=lambda: ["*"])

    # -- Security ---------------------------------------------------------
    secret_key: str = "insecure-development-key-change-me"

    # -- Database ---------------------------------------------------------
    database_url: str = "postgresql+psycopg://tenderbase:tenderbase@localhost:5432/tenderbase"
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_echo: bool = False

    # -- Redis / workers --------------------------------------------------
    redis_url: str = "redis://localhost:6379/0"

    # -- HTTP / ingestion -------------------------------------------------
    http_timeout_seconds: float = 30.0
    http_max_retries: int = 3
    http_backoff_base_seconds: float = 0.5
    http_max_response_bytes: int = 25 * 1024 * 1024
    http_max_redirects: int = 5
    http_user_agent: str = "TenderBaseBot/0.1 (+https://tenderbase.example/bot)"
    http_respect_robots: bool = True
    http_default_rate_limit_per_minute: int = 30
    http_allow_private_networks: bool = False

    # -- Documents --------------------------------------------------------
    document_storage_backend: Literal["local", "s3"] = "local"
    document_storage_path: Path = Path("./data/documents")
    document_max_bytes: int = 100 * 1024 * 1024
    raw_payload_storage_path: Path = Path("./data/raw")
    raw_payload_inline_max_bytes: int = 64 * 1024

    # -- OCR --------------------------------------------------------------
    ocr_enabled: bool = False
    ocr_languages: str = "eng"

    # -- AI (optional) ----------------------------------------------------
    ai_enabled: bool = False
    ai_provider: Literal["null", "openai", "anthropic"] = "null"
    ai_api_key: str | None = None
    ai_model: str | None = None

    # -- Pagination -------------------------------------------------------
    default_page_size: int = 25
    max_page_size: int = 100

    # -- Rate limiting (extension point) ----------------------------------
    statistics_cache_seconds: float = Field(
        default=60.0,
        ge=0,
        description="In-process TTL for the statistics endpoint; 0 disables caching.",
    )
    rate_limit_enabled: bool = False
    rate_limit_anonymous_per_minute: int = 60

    # -- Validators -------------------------------------------------------
    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip().startswith("["):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("log_level")
    @classmethod
    def _upper_log_level(cls, value: str) -> str:
        level = value.upper()
        if level not in {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"}:
            raise ValueError(f"invalid LOG_LEVEL: {value}")
        return level

    @field_validator("database_url")
    @classmethod
    def _validate_database_url(cls, value: str) -> str:
        if "://" not in value:
            raise ValueError("DATABASE_URL must be a SQLAlchemy URL")
        return value

    @model_validator(mode="after")
    def _production_guards(self) -> Settings:
        if self.app_env == "production":
            if (
                self.secret_key.startswith("insecure")
                or self.secret_key == "change-me-in-production"
            ):
                raise ValueError("SECRET_KEY must be set to a strong value in production")
            if self.debug:
                raise ValueError("DEBUG must be false in production")
        if self.ai_enabled and self.ai_provider != "null" and not self.ai_api_key:
            raise ValueError("AI_API_KEY is required when AI_ENABLED=true with a real provider")
        return self

    # -- Derived helpers --------------------------------------------------
    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def api_v1_prefix(self) -> str:
        return f"{self.api_prefix.rstrip('/')}/v1"

    @property
    def sync_database_url(self) -> str:
        """Synchronous URL (used by Alembic and sync tooling)."""
        return (
            self.database_url.replace("+asyncpg", "")
            .replace("+psycopg_async", "+psycopg")
            .replace("+aiosqlite", "")
        )

    @property
    def async_database_url(self) -> str:
        """Async URL used by the application engine."""
        url = self.database_url
        if url.startswith("postgresql://"):
            return url.replace("postgresql://", "postgresql+psycopg://", 1)
        if url.startswith("sqlite://") and "+aiosqlite" not in url:
            return url.replace("sqlite://", "sqlite+aiosqlite://", 1)
        return url


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    return Settings()


settings = get_settings()
