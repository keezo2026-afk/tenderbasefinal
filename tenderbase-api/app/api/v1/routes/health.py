"""Health endpoints: overall, liveness and readiness."""

from __future__ import annotations

import time

from fastapi import APIRouter, Response, status
from sqlalchemy import text

from app.api.dependencies import SessionDep, SettingsDep
from app.schemas.common import HealthComponent, HealthResponse
from app.utils.dates import utcnow

router = APIRouter(tags=["health"])


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Service health",
    description=(
        "Reports overall service health, including database connectivity. "
        "Returns HTTP 503 when a critical dependency is unavailable."
    ),
    responses={503: {"description": "A critical dependency is unhealthy"}},
)
async def health(session: SessionDep, settings: SettingsDep, response: Response) -> HealthResponse:
    components = [await _check_database(session)]
    unhealthy = [c for c in components if c.status != "healthy"]
    if unhealthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return HealthResponse(
        status="degraded" if unhealthy else "healthy",
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
    ready = database.status == "healthy"
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return HealthResponse(
        status="ready" if ready else "not_ready",
        app=settings.app_name,
        version=settings.app_version,
        environment=settings.app_env,
        time=utcnow(),
        components=[database],
    )


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
        )
    return HealthComponent(
        name="database",
        status="healthy",
        latency_ms=round((time.perf_counter() - started) * 1000, 2),
    )
