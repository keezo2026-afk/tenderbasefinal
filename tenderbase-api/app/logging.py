"""Structured logging.

Logs carry contextual fields (``request_id``, ``source_id``, ``job_id``,
``connector`` ...) so ingestion and API problems can be traced end to end.
Secrets are redacted before emission.
"""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import Mapping, MutableMapping
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


def _redact(_logger: Any, _method: str, event_dict: MutableMapping[str, Any]) -> Mapping[str, Any]:
    """Redact obvious secrets from log payloads."""
    for key, value in list(event_dict.items()):
        if _SECRET_KEY_PATTERN.search(key):
            event_dict[key] = _REDACTED
        elif isinstance(value, str) and _SECRET_VALUE_PATTERN.search(value):
            event_dict[key] = _SECRET_VALUE_PATTERN.sub(
                lambda m: f"{m.group('prefix')}{_REDACTED}", value
            )
    return event_dict


def _add_context(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> Mapping[str, Any]:
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


class _ForwardingStream:
    """A file-like object that resolves its destination at write time.

    structlog caches a bound logger on first use (and modules create theirs at
    import time, long before an entrypoint calls ``configure_logging``), so a
    stream captured at that moment is frozen for the life of the process — which
    is how log lines ended up on stdout in a script that asked for stderr, and how
    lines emitted before configuration skipped the redaction processor entirely.
    Forwarding through this object makes "where logs go" a property of the process,
    re-decidable at any point, instead of a property of import order.
    """

    encoding = "utf-8"

    def write(self, message: str) -> int:
        return _STREAM[0].write(message)

    def flush(self) -> None:
        _STREAM[0].flush()

    def isatty(self) -> bool:
        try:
            return bool(_STREAM[0].isatty())
        except Exception:  # noqa: BLE001 - a missing capability is not an error
            return False


#: Current log destination and renderer choice, resolved per write.
_STREAM: list[Any] = [sys.stdout]
_JSON_LOGS: list[bool] = [False]

#: Every application logger lives under this namespace so the level can be tuned for
#: the application alone, without touching third-party libraries sharing the root logger.
LOGGER_NAMESPACE = "tenderbase"


def _render(_logger: Any, _method: str, event_dict: MutableMapping[str, Any]) -> str:
    """Render with the *currently* configured format, not the one at import time."""
    renderer = (
        structlog.processors.JSONRenderer()
        if _JSON_LOGS[0]
        else structlog.dev.ConsoleRenderer(colors=False)
    )
    rendered = renderer(_logger, _method, event_dict)
    return rendered if isinstance(rendered, str) else rendered.decode("utf-8", "replace")


def configure_logging(settings: Settings | None = None, *, stream: Any = None) -> None:
    """Configure stdlib logging + structlog once, at process start.

    Two properties are deliberate, because modules call ``get_logger()`` at import time
    and structlog then caches the bound logger:

    * the destination is resolved per write (``_ForwardingStream``), and
    * filtering is delegated to stdlib logging (``filter_by_level``) instead of a level
      baked into the wrapper class, so a later call — including a level change — still
      governs loggers created before it.

    Without both, the configuration an entrypoint applies *after* imports only half
    works, which in this codebase showed up as script log lines landing on stdout ahead
    of a ``--json`` document, and as early-imported modules logging without redaction.

    ``stream`` chooses where log lines are written. Long-running services leave it
    unset: stdout is what container runtimes collect. **Scripts must pass
    ``sys.stderr``** — their stdout is the machine-readable result (``--json``), and
    one interleaved timestamp makes the whole stream unparsable, so the operator
    pipes a report into ``jq`` and gets an error instead of data.
    """
    cfg = settings or get_settings()
    _STREAM[0] = sys.stdout if stream is None else stream
    _JSON_LOGS[0] = bool(cfg.log_json or cfg.is_production)

    level = getattr(logging, cfg.log_level, logging.INFO)
    logging.basicConfig(format="%(message)s", stream=_ForwardingStream(), level=level, force=True)
    logging.getLogger(LOGGER_NAMESPACE).setLevel(level)
    for noisy in ("uvicorn.access", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(max(logging.INFO, level))

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_logger_name,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _add_context,
            _redact,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            _render,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None, **initial: Any) -> structlog.stdlib.BoundLogger:
    """Return a bound structlog logger tagged with the service name.

    Names are kept inside :data:`LOGGER_NAMESPACE` so that tuning the application's
    verbosity does not mean tuning every library in the process.
    """
    logger_name = name or LOGGER_NAMESPACE
    if logger_name != LOGGER_NAMESPACE and not logger_name.startswith(f"{LOGGER_NAMESPACE}."):
        logger_name = f"{LOGGER_NAMESPACE}.{logger_name}"
    logger = structlog.get_logger(logger_name)
    return logger.bind(service=get_settings().app_name, **initial)


# Configured at import, deliberately: every module creates its logger with
# ``get_logger()`` at import time, and any logger built before configuration would
# otherwise keep structlog's default processors — no redaction, no request_id,
# console format in a JSON deployment. ``configure_logging`` may still be called
# later (a test, a script choosing stderr, a settings object with a new level).
configure_logging()
