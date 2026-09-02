"""API v1 router aggregation."""

from fastapi import APIRouter

from app.api.v1.routes import (
    categories,
    documents,
    events,
    health,
    municipalities,
    provinces,
    search,
    sources,
    statistics,
    tenders,
)

api_router = APIRouter()

api_router.include_router(health.router)
api_router.include_router(tenders.router)
api_router.include_router(search.router)
api_router.include_router(municipalities.router)
api_router.include_router(provinces.router)
api_router.include_router(sources.router)
api_router.include_router(documents.router)
api_router.include_router(categories.router)
api_router.include_router(events.router)
api_router.include_router(statistics.router)

__all__ = ["api_router"]
