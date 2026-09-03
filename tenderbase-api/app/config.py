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
    #: Configuration-driven CORS. There is **no** browser client in this
    #: repository, so the default is empty: cross-origin requests are refused
    #: unless an operator explicitly lists trusted origins.
    cors_origins: list[str] = Field(default_factory=list)

    # -- Security ---------------------------------------------------------
    secret_key: str = "insecure-development-key-change-me"
    #: Used as the HMAC pepper for API-key digests. Rotating it invalidates
    #: every issued key, which is the desired fail-closed behaviour.
    api_key_pepper: str | None = None
    api_key_header: str = "X-API-Key"
    #: When true, every data endpoint requires a valid, correctly scoped key.
    #: Defaults to true in production (enforced below) and false elsewhere so
    #: local development and the unauthenticated smoke tests keep working.
    api_key_enforcement_enabled: bool | None = None
    #: Mint keys through the HTTP API? Off by default: key creation is an
    #: audited operator action (``scripts/manage_api_keys.py``), and an API that
    #: can hand out credentials is an API whose leak becomes a credential leak.
    api_key_self_service_enabled: bool = False
    #: Health endpoints are always unauthenticated — container orchestrators and
    #: load balancers cannot be expected to hold credentials.
    allow_unauthenticated_health: bool = True
    #: Metrics endpoint configuration. Excluded from the public OpenAPI schema
    #: and optionally protected by a separate bearer token.
    metrics_enabled: bool = True
    metrics_path: str = "/metrics"
    metrics_token: str | None = None

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
    #: Ports an outbound URL may name. The default is the small set of web ports
    #: municipalities actually use; a source on a non-standard port is refused
    #: rather than silently fetched, because an unexpected port is exactly what
    #: an SSRF attempt through a compromised listing page looks like. Widen this
    #: per deployment (and record why) instead of loosening the guard.
    http_allowed_ports: str = "80,443,8080,8443"

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

    # -- Rate limiting ----------------------------------------------------
    #: Redis-backed sliding-window limiter. Falls back to a bounded in-process
    #: window when Redis is unavailable (``rate_limit_fail_open`` decides what
    #: happens then) so a Redis outage never takes the read API down.
    rate_limit_enabled: bool | None = None
    rate_limit_anonymous_per_minute: int = 20
    rate_limit_authenticated_per_minute: int = 60
    rate_limit_admin_per_minute: int = 600
    #: Extra requests allowed above the sustained rate within one window.
    rate_limit_burst: int = 20
    #: When Redis cannot be reached: fail open (serve traffic, log loudly) or
    #: fail closed (reject). Serving a read-only API without its cache is the
    #: safer default for availability; deployments with stricter requirements
    #: can set this to false.
    rate_limit_fail_open: bool = True
    #: In-process fallback window size (entries); oldest evicted, bounded RAM.
    rate_limit_fallback_max_entries: int = 10_000
    #: In-process TTL for the statistics endpoint; 0 disables caching.
    statistics_cache_seconds: float = Field(default=60.0, ge=0)

    # -- Workers / queue --------------------------------------------------
    #: Hard cap on retries for a single ingestion job (ARQ ``max_tries``).
    worker_max_tries: int = Field(default=3, ge=1, le=10)
    #: Base delay for ARQ's exponential retry backoff, in seconds.
    worker_retry_backoff_seconds: float = Field(default=5.0, ge=0.1)
    #: Concurrent jobs per worker process.
    worker_max_jobs: int = Field(default=5, ge=1, le=64)
    #: Per-job wall-clock timeout in seconds.
    worker_job_timeout_seconds: int = Field(default=900, ge=10)
    #: How long ARQ keeps job results in Redis for inspection.
    worker_keep_result_seconds: int = Field(default=3600, ge=0)

    # -- Validators -------------------------------------------------------
    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip().startswith("["):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("http_allowed_ports")
    @classmethod
    def _parse_ports(cls, value: str) -> str:
        kept: list[int] = []
        for part in value.replace(";", ",").split(","):
            candidate = part.strip()
            if not candidate:
                continue
            if not candidate.isdigit() or not (1 <= int(candidate) <= 65535):
                raise ValueError(f"HTTP_ALLOWED_PORTS must be a comma list of ports, got {part!r}")
            if int(candidate) not in kept:
                kept.append(int(candidate))
        if not kept:
            raise ValueError("HTTP_ALLOWED_PORTS must list at least one port")
        return ",".join(str(port) for port in sorted(kept))

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

    @field_validator("api_key_header")
    @classmethod
    def _validate_header_name(cls, value: str) -> str:
        """A header name is inserted into responses and looked up verbatim.

        Reject anything that could be used for response/log header injection.
        """
        name = value.strip()
        if not name or any(c in name for c in "\r\n: "):
            raise ValueError("API_KEY_HEADER must be a bare HTTP header name")
        return name

    @field_validator("metrics_path")
    @classmethod
    def _validate_metrics_path(cls, value: str) -> str:
        path = value.strip()
        if not path.startswith("/"):
            raise ValueError("METRICS_PATH must start with '/'")
        return path

    @model_validator(mode="after")
    def _production_guards(self) -> Settings:
        """Refuse to boot production on insecure defaults.

        These are hard failures at *startup*, not runtime warnings: a
        misconfigured TenderBase should not be running at all rather than
        serving traffic with placeholder credentials.
        """
        if self.app_env == "production":
            if (
                self.secret_key.startswith("insecure")
                or self.secret_key == "change-me-in-production"
            ):
                raise ValueError("SECRET_KEY must be set to a strong value in production")
            if len(self.secret_key) < 32:
                raise ValueError("SECRET_KEY must be at least 32 characters in production")
            if self.debug:
                raise ValueError("DEBUG must be false in production")
            if not self.database_url.startswith("postgresql"):
                raise ValueError(
                    "DATABASE_URL must point at PostgreSQL in production "
                    "(SQLite is a development/test convenience only)"
                )
            if self.api_key_enforcement_enabled is False:
                raise ValueError("API_KEY_ENFORCEMENT_ENABLED cannot be disabled in production")
            if self.rate_limit_enabled is False:
                raise ValueError("RATE_LIMIT_ENABLED cannot be disabled in production")
            if "*" in self.cors_origins:
                raise ValueError("CORS_ORIGINS must not contain '*' in production")
            if not self.log_json:
                raise ValueError("LOG_JSON must be true in production for log aggregation")
        if self.ai_enabled and self.ai_provider != "null" and not self.ai_api_key:
            raise ValueError("AI_API_KEY is required when AI_ENABLED=true with a real provider")
        return self

    # -- Resolved defaults ------------------------------------------------

    @property
    def enforce_api_keys(self) -> bool:
        """Production/staging authenticate unless explicitly disabled.

        Development, test and local deployments may leave the read API open for
        convenience; production may not (see ``_production_guards``).
        """
        if self.api_key_enforcement_enabled is None:
            return self.app_env in ("production", "staging")
        return self.api_key_enforcement_enabled

    @property
    def use_rate_limit(self) -> bool:
        if self.rate_limit_enabled is None:
            return self.app_env in ("production", "staging")
        return self.rate_limit_enabled

    @property
    def allowed_ports(self) -> frozenset[int]:
        """Parsed :attr:`http_allowed_ports`, for :func:`app.utils.urls.validate_url`."""
        return frozenset(int(part) for part in self.http_allowed_ports.split(",") if part)

    @property
    def key_pepper(self) -> str:
        """Pepper for API-key digests: ``API_KEY_PEPPER`` or ``SECRET_KEY``.

        A deployment that wants issued keys to survive ``SECRET_KEY`` rotation
        sets ``API_KEY_PEPPER`` once and keeps it stable.
        """
        return self.api_key_pepper or self.secret_key

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
