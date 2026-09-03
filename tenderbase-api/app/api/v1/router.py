"""API v1 router aggregation.

Health probes are the only routes without the authentication dependency: an
orchestrator (Kubernetes, ECS, Fly, a load balancer) cannot present a
credential, and blocking it turns a healthy service into a false outage.

``/metrics`` is mounted by the application factory, not here — it is an internal
operations surface and is kept out of the public OpenAPI schema on purpose.
"""

from fastapi import APIRouter, Depends

from app.api.auth import api_access
from app.api.v1.routes import (
    api_keys,
    categories,
    documents,
    events,
    health,
    municipalities,
    operations,
    provinces,
    search,
    sources,
    statistics,
    tenders,
)

#: Every data endpoint is authenticated + scope-checked + rate-limited.
PROTECTED = [Depends(api_access)]

api_router = APIRouter()

api_router.include_router(health.router)
api_router.include_router(tenders.router, dependencies=PROTECTED)
api_router.include_router(search.router, dependencies=PROTECTED)
api_router.include_router(municipalities.router, dependencies=PROTECTED)
api_router.include_router(provinces.router, dependencies=PROTECTED)
api_router.include_router(sources.router, dependencies=PROTECTED)
api_router.include_router(documents.router, dependencies=PROTECTED)
api_router.include_router(categories.router, dependencies=PROTECTED)
api_router.include_router(events.router, dependencies=PROTECTED)
api_router.include_router(statistics.router, dependencies=PROTECTED)
api_router.include_router(api_keys.router, dependencies=PROTECTED)
api_router.include_router(operations.router, dependencies=PROTECTED)

__all__ = ["api_router"]
