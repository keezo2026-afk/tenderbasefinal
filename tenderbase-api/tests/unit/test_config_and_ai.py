"""Unit tests for configuration, logging redaction and the AI abstraction."""

from __future__ import annotations

import pytest

from app.ai import get_ai_provider
from app.ai.provider import NullAIProvider, ai_status
from app.config import Settings
from app.errors import AIUnavailableError
from app.logging import _redact


def test_settings_derive_async_and_sync_urls():
    settings = Settings(database_url="postgresql://u:p@db:5432/tenderbase", secret_key="x" * 32)
    assert settings.async_database_url.startswith("postgresql+psycopg://")
    assert "+psycopg" in settings.sync_database_url or settings.sync_database_url.startswith(
        "postgresql://"
    )
    assert settings.api_v1_prefix == "/api/v1"


def test_production_requires_a_real_secret_key():
    with pytest.raises(ValueError):
        Settings(app_env="production", secret_key="change-me-in-production", debug=False)


def test_production_forbids_debug():
    with pytest.raises(ValueError):
        Settings(app_env="production", secret_key="s" * 40, debug=True)


def test_ai_credentials_required_only_when_ai_enabled():
    # The core API must start without any AI configuration.
    settings = Settings(secret_key="s" * 40)
    assert settings.ai_enabled is False

    with pytest.raises(ValueError):
        Settings(secret_key="s" * 40, ai_enabled=True, ai_provider="openai", ai_api_key=None)


def test_cors_origins_accept_comma_separated_values():
    settings = Settings(secret_key="s" * 40, cors_origins="https://a.example,https://b.example")
    assert settings.cors_origins == ["https://a.example", "https://b.example"]


def test_log_redaction_hides_secrets():
    event = _redact(None, "info", {"api_key": "super-secret", "msg": "password=hunter2"})
    assert event["api_key"] == "***redacted***"
    assert "hunter2" not in event["msg"]


async def test_ai_disabled_returns_null_provider_that_never_fabricates():
    provider = get_ai_provider(Settings(secret_key="s" * 40, ai_enabled=False))
    assert isinstance(provider, NullAIProvider)
    assert provider.available is False
    with pytest.raises(AIUnavailableError):
        await provider.summarize("some text")


def test_ai_status_reports_availability():
    status = ai_status(Settings(secret_key="s" * 40))
    assert status == {"enabled": False, "provider": "null", "available": False, "model": None}
