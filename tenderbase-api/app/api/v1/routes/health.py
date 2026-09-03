"""Health endpoints: overall, liveness and readiness.

Three probes, mounted both under ``/api/v1`` (the canonical paths advertised
in the OpenAPI document) and at the application root, because container
orchestrators and load balancers are frequently configured to probe ``/``.

* ``/health`` — every dependency, best-effort; 503 only when something is
  genuinely down.
* ``/health/live`` — no I/O at all. A process that cannot answer this should be
  killed, but a process whose *database* is down must not be, or Kubernetes
  turns one outage into a crashloop.
* ``/health/ready`` — the dependencies this instance cannot serve traffic
  without: the database always, Redis when it is load-bearing (rate limiting
  or background ingestion).
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from fastapi import APIRouter, Response, status
from sqlalchemy import text
from structlog import get_logger

from app.api.dependencies import SessionDep, SettingsDep
from app.schemas.common import HealthComponent, HealthResponse
from app.utils.dates import utcnow

if TYPE_CHECKING:  # pragma: no cover
    from app.config import Settings

router = APIRouter(tags=["health"])
logger = get_logger("health")

#: How long we are willing to wait for Redis before calling it unhealthy. A
#: health probe must never become the thing that takes a pod down.
_REDIS_TIMEOUT_SECONDS = 1.5

#: Statuses that count as "nothing to report".
_OK_STATUSES = frozenset({"healthy", "disabled"})


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Service health",
    description=(
        "Reports every dependency this process knows about, with latency. "
        "Returns HTTP 503 when a *required* dependency is unavailable; an "
        "optional one is reported without changing the verdict."
    ),
    responses={503: {"description": "A critical dependency is unhealthy"}},
)
async def health(session: SessionDep, settings: SettingsDep, response: Response) -> HealthResponse:
    components = [await _check_database(session), await _check_cache(settings)]
    down = [c for c in components if c.required and c.status != "healthy"]
    soft = [c for c in components if not c.required and c.status not in _OK_STATUSES]
    if down:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    overall = "unhealthy" if down else ("degraded" if soft else "healthy")
    return HealthResponse(
        status=overall,
        app=settings.app_name,
        version=settings.app_version,
        environment=settings.app_env,
        time=utcnow(),
        components=components,
    )


@router.get(
    "/health/live",
    response_model=HealthResponse,
    summary="Liveness probe",
    description="Returns 200 whenever the process is running. Performs no I/O.",
)
async def liveness(settings: SettingsDep) -> HealthResponse:
    return HealthResponse(
        status="healthy",
        app=settings.app_name,
        version=settings.app_version,
        environment=settings.app_env,
        time=utcnow(),
        components=[],
    )


@router.get(
    "/health/ready",
    response_model=HealthResponse,
    summary="Readiness probe",
    description=(
        "Returns 200 only when the service can serve traffic — i.e. the "
        "database is reachable. Use this for load-balancer readiness checks."
    ),
    responses={503: {"description": "Not ready to serve traffic"}},
)
async def readiness(
    session: SessionDep, settings: SettingsDep, response: Response
) -> HealthResponse:
    database = await _check_database(session)
    cache = await _check_cache(settings)
    components = [database, cache]
    # A dependency that is merely optional is reported but never blocks traffic.
    blocking = [c for c in components if c.required and c.status != "healthy"]
    if blocking:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return HealthResponse(
        status="ready" if not blocking else "not_ready",
        app=settings.app_name,
        version=settings.app_version,
        environment=settings.app_env,
        time=utcnow(),
        components=components,
    )


def _cache_required(settings: Settings) -> bool:
    """Is Redis something this process needs in order to serve requests correctly?

    Only when the limiter has been told it may not degrade. With the default
    ``RATE_LIMIT_FAIL_OPEN=true`` a Redis outage moves enforcement to the
    in-process limiter — weaker (per replica) but functional, and
    ``X-RateLimit-Policy`` says so on every response — so making readiness depend
    on Redis would take the API out of rotation *because of an outage of an
    optional component*, and a load balancer would restart a healthy process into
    a crash loop during the incident.

    With ``RATE_LIMIT_FAIL_OPEN=false`` the opposite holds: protected requests are
    answered 503 (see :class:`ResilientRateLimiter`), so readiness has to fail or
    it would advertise a node that refuses every data request.
    """
    return settings.use_rate_limit and not settings.rate_limit_fail_open


async def _check_cache(settings: Settings) -> HealthComponent:
    """PING the Redis instance backing rate limits and the work queue.

    Never raises and never waits longer than :data:`_REDIS_TIMEOUT_SECONDS`:
    a probe that hangs is worse than a probe that reports a failure.
    """
    started = time.perf_counter()
    required = _cache_required(settings)

    def done(component_status: str, detail: str | None = None) -> HealthComponent:
        return HealthComponent(
            name="cache",
            status=component_status,
            detail=detail,
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
            required=required,
        )

    if not settings.use_rate_limit:
        # Rate limiting is off, so nothing here depends on Redis at all: skip the
        # probe rather than make the health endpoints hinge on a service the
        # deployment may legitimately not run. When rate limiting *is* on but may
        # degrade, the probe still runs — an operator needs to see "degraded" in
        # the body, because a fallback limiter is a real loss of global
        # enforcement even though it must not block traffic.
        return done("disabled", "rate limiting is disabled")

    try:
        import redis.asyncio as aioredis
    except ImportError:  # pragma: no cover - depends on the install extras
        return done("unhealthy" if required else "degraded", "redis client not installed")

    client = None
    try:
        client = aioredis.from_url(
            settings.redis_url,
            socket_connect_timeout=_REDIS_TIMEOUT_SECONDS,
            socket_timeout=_REDIS_TIMEOUT_SECONDS,
            # Health probes must not consume application pool connections.
            max_connections=1,
        )
        pong = await client.ping()
    except Exception as exc:  # noqa: BLE001 - a health check must never raise
        logger.debug("cache_health_check_failed", error=str(exc))
        return done("unhealthy" if required else "degraded", type(exc).__name__)
    finally:
        if client is not None:
            try:
                await client.aclose()
            except Exception:  # noqa: BLE001 - closing must not change the verdict
                pass
    if not pong:
        return done("unhealthy" if required else "degraded", "PING returned no reply")
    return done("healthy")


async def _check_database(session: SessionDep) -> HealthComponent:
    started = time.perf_counter()
    try:
        await session.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 - health must never raise
        return HealthComponent(
            name="database",
            status="unhealthy",
            detail=type(exc).__name__,
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
            required=True,
        )
    return HealthComponent(
        name="database",
        status="healthy",
        latency_ms=round((time.perf_counter() - started) * 1000, 2),
        required=True,
    )
