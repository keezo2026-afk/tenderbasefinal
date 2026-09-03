"""HTTP middleware: request IDs, access logging, rate-limit headers, security headers.

Responsibilities, in order of what would hurt most if it were missing:

* **Correlation** — every request gets (or keeps) an ``X-Request-ID`` that is
  bound to the log context so a slow endpoint can be traced to the ingestion or
  database work behind it.
* **Response headers** — security hardening headers plus the ``X-RateLimit-*``
  headers the limiter decided on, attached here so no route has to remember.
* **Access log** — method, path, status, duration and the authenticated caller's
  *prefix* (never its key). Health probes are excluded: they would otherwise be
  the loudest thing in the log and hide real traffic.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.logging import get_logger, request_id_ctx
from app.observability import metrics

REQUEST_ID_HEADER = "X-Request-ID"
logger = get_logger("tenderbase.http")

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; img-src 'self' data:",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
}

#: Probes would dominate the log and carry no information.
QUIET_PATHS = ("/health", "/health/live", "/health/ready", "/metrics")


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assigns a request ID, logs the request and adds security headers."""

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        incoming = request.headers.get(REQUEST_ID_HEADER, "")
        request_id = _sanitize(incoming) or uuid.uuid4().hex
        request.state.request_id = request_id
        token = request_id_ctx.set(request_id)
        started = time.perf_counter()
        metrics.HTTP_INPROGRESS.labels(method=request.method).inc()

        try:
            response = await call_next(request)
        except Exception:
            duration = (time.perf_counter() - started) * 1000
            metrics.observe_http_request(
                method=request.method,
                route=metrics.route_label(request),
                status_code=500,
                duration_seconds=duration / 1000,
            )
            logger.exception(
                "http.request_failed",
                method=request.method,
                path=request.url.path,
                duration=round(duration, 2),
            )
            raise
        finally:
            request_id_ctx.reset(token)
            metrics.HTTP_INPROGRESS.labels(method=request.method).dec()

        duration = (time.perf_counter() - started) * 1000
        metrics.observe_http_request(
            method=request.method,
            route=metrics.route_label(request),
            status_code=response.status_code,
            duration_seconds=duration / 1000,
        )
        response.headers[REQUEST_ID_HEADER] = request_id
        for header, value in SECURITY_HEADERS.items():
            response.headers.setdefault(header, value)

        # Rate-limit headers are attached centrally: the limiter runs inside an
        # auth dependency, so a route that forgets them still reports correctly.
        decision = getattr(request.state, "rate_limit", None)
        if decision is not None:
            for header, value in decision.headers().items():
                response.headers.setdefault(header, value)

        if request.url.path not in QUIET_PATHS:
            principal = getattr(request.state, "principal", None)
            logger.info(
                "http.request",
                method=request.method,
                path=request.url.path,
                query=str(request.url.query)[:500] or None,
                status=response.status_code,
                duration=round(duration, 2),
                api_key_prefix=getattr(principal, "display_id", None),
                error_code=getattr(request.state, "auth_error", None),
            )
        return response


def _sanitize(value: str) -> str | None:
    """Allow only short, safe request IDs from clients (log-injection guard)."""
    candidate = "".join(ch for ch in value.strip() if ch.isalnum() or ch in "-_")[:64]
    return candidate or None


class PublicRateLimitMiddleware(BaseHTTPMiddleware):
    """Coarse per-IP limit for the *unauthenticated* endpoints only.

    ``/health``, ``/metrics``, the OpenAPI documents and the root banner are
    deliberately public, so they need *some* ceiling against a hammering client.
    Authenticated data endpoints are limited inside the auth dependency, where
    the caller's identity (and therefore the right tier) is known — applying a
    second limit here would silently make an authenticated client's budget the
    anonymous one.
    """

    PROTECTED_BY_AUTH = ("/api/v1/tenders", "/api/v1/search", "/api/v1/sources")

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        settings = getattr(request.app.state, "settings", None)
        limiter = None
        if settings is not None and settings.use_rate_limit:
            from app.services.rate_limit import get_limiter, policy_for

            limiter = get_limiter()
            path = request.url.path
            is_public = not path.startswith(self.PROTECTED_BY_AUTH) and not any(
                path.startswith(f"/api/v1/{name}")
                for name in (
                    "municipalities",
                    "provinces",
                    "documents",
                    "categories",
                    "events",
                    "statistics",
                    "api-keys",
                    "operations",
                )
            )
            if limiter is not None and is_public:
                limit, burst = policy_for("anonymous", settings)
                client = request.client.host if request.client else "unknown"
                try:
                    decision = await limiter.check(f"anonymous:{client}", limit=limit, burst=burst)
                except Exception as exc:  # noqa: BLE001 - fail-closed policy is the limiter's call
                    logger.error("http.rate_limit_check_failed", error=str(exc))
                    decision = None
                if decision is not None:
                    request.state.rate_limit = decision
                    if not decision.allowed:
                        from app.errors import RateLimitedError

                        raise RateLimitedError(
                            "Rate limit exceeded for unauthenticated requests.",
                            code="RATE_LIMITED",
                            details={"retry_after_seconds": decision.retry_after_seconds},
                            headers={"Retry-After": str(max(decision.retry_after_seconds, 1))},
                        )
        return await call_next(request)
