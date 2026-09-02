"""Structured logging.

Logs carry contextual fields (``request_id``, ``source_id``, ``job_id``,
``connector`` ...) so ingestion and API problems can be traced end to end.
Secrets are redacted before emission.
"""

from __future__ import annotations

import logging
import re
import sys
from contextvars import ContextVar
from typing import Any

import structlog

from app.config import Settings, get_settings

request_id_ctx: ContextVar[str | None] = ContextVar("request_id", default=None)
job_id_ctx: ContextVar[str | None] = ContextVar("job_id", default=None)
source_id_ctx: ContextVar[str | None] = ContextVar("source_id", default=None)

_SECRET_KEY_PATTERN = re.compile(r"(?i)(api[_-]?key|secret|password|token|authorization|passwd)")
_SECRET_VALUE_PATTERN = re.compile(
    r"(?i)(?P<prefix>(?:api[_-]?key|secret|password|token)\"?\s*[:=]\s*)(?P<value>[^\s,;\"']+)"
)
_REDACTED = "***redacted***"


def _redact(_logger: Any, _method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """Redact obvious secrets from log payloads."""
    for key, value in list(event_dict.items()):
        if _SECRET_KEY_PATTERN.search(key):
            event_dict[key] = _REDACTED
        elif isinstance(value, str) and _SECRET_VALUE_PATTERN.search(value):
            event_dict[key] = _SECRET_VALUE_PATTERN.sub(
                lambda m: f"{m.group('prefix')}{_REDACTED}", value
            )
    return event_dict


def _add_context(_logger: Any, _method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """Attach ambient context variables to every log line."""
    for name, ctx in (
        ("request_id", request_id_ctx),
        ("job_id", job_id_ctx),
        ("source_id", source_id_ctx),
    ):
        value = ctx.get()
        if value and name not in event_dict:
            event_dict[name] = value
    return event_dict


def configure_logging(settings: Settings | None = None) -> None:
    """Configure stdlib logging + structlog once, at process start."""
    cfg = settings or get_settings()

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, cfg.log_level, logging.INFO),
        force=True,
    )
    for noisy in ("uvicorn.access", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(max(logging.INFO, getattr(logging, cfg.log_level)))

    renderer: Any = (
        structlog.processors.JSONRenderer()
        if cfg.log_json or cfg.is_production
        else structlog.dev.ConsoleRenderer(colors=False)
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _add_context,
            _redact,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, cfg.log_level, logging.INFO)
        ),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None, **initial: Any) -> structlog.stdlib.BoundLogger:
    """Return a bound structlog logger tagged with the service name."""
    logger = structlog.get_logger(name or "tenderbase")
    return logger.bind(service=get_settings().app_name, **initial)
