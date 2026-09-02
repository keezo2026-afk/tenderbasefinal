"""HTTP middleware: request IDs, access logging and security headers."""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.logging import get_logger, request_id_ctx

REQUEST_ID_HEADER = "X-Request-ID"
logger = get_logger("tenderbase.http")

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
}


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assigns a request ID, logs the request and adds security headers.

    The request ID is accepted from the client (for distributed tracing) but
    sanitised, echoed in the response headers and bound to every log line.
    """

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        incoming = request.headers.get(REQUEST_ID_HEADER, "")
        request_id = _sanitize(incoming) or uuid.uuid4().hex
        request.state.request_id = request_id
        token = request_id_ctx.set(request_id)
        started = time.perf_counter()

        try:
            response = await call_next(request)
        except Exception:
            duration = (time.perf_counter() - started) * 1000
            logger.exception(
                "http.request_failed",
                method=request.method,
                path=request.url.path,
                duration=round(duration, 2),
            )
            raise
        finally:
            request_id_ctx.reset(token)

        duration = (time.perf_counter() - started) * 1000
        response.headers[REQUEST_ID_HEADER] = request_id
        for header, value in SECURITY_HEADERS.items():
            response.headers.setdefault(header, value)

        # Health probes would otherwise dominate the logs.
        if not request.url.path.endswith(("/health", "/health/live", "/health/ready")):
            logger.info(
                "http.request",
                method=request.method,
                path=request.url.path,
                query=str(request.url.query)[:500] or None,
                status=response.status_code,
                duration=round(duration, 2),
            )
        return response


def _sanitize(value: str) -> str | None:
    """Allow only short, safe request IDs from clients (log-injection guard)."""
    candidate = "".join(ch for ch in value.strip() if ch.isalnum() or ch in "-_")[:64]
    return candidate or None
