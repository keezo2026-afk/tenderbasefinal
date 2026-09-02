"""Shared FastAPI dependencies: sessions, pagination, services and auth hooks."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db.session import get_session
from app.schemas.common import PaginationParams, ResponseMeta
from app.services.document_service import DocumentService
from app.services.municipality_service import MunicipalityService
from app.services.search_service import SearchService
from app.services.source_service import SourceService
from app.services.statistics_service import StatisticsService
from app.services.tender_service import TenderService
from app.utils.dates import utcnow

SessionDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


def get_pagination(
    page: Annotated[int, Query(ge=1, le=10_000, description="1-based page number")] = 1,
    page_size: Annotated[
        int, Query(ge=1, le=100, alias="page_size", description="Items per page")
    ] = 25,
    settings: SettingsDep = None,  # type: ignore[assignment]
) -> PaginationParams:
    """Validated pagination parameters, clamped to the configured maximum."""
    cfg = settings or get_settings()
    return PaginationParams(page=page, page_size=min(page_size, cfg.max_page_size))


PaginationDep = Annotated[PaginationParams, Depends(get_pagination)]


def get_request_id(request: Request) -> str | None:
    """The current request's correlation ID."""
    return getattr(request.state, "request_id", None)


RequestIdDep = Annotated[str | None, Depends(get_request_id)]


def response_meta(request_id: RequestIdDep) -> ResponseMeta:
    """Standard ``meta`` block attached to responses."""
    return ResponseMeta(request_id=request_id, generated_at=utcnow())


MetaDep = Annotated[ResponseMeta, Depends(response_meta)]


# --- Services -------------------------------------------------------------


def get_tender_service(session: SessionDep) -> TenderService:
    return TenderService(session)


def get_search_service(session: SessionDep) -> SearchService:
    return SearchService(session)


def get_municipality_service(session: SessionDep) -> MunicipalityService:
    return MunicipalityService(session)


def get_source_service(session: SessionDep) -> SourceService:
    return SourceService(session)


def get_document_service(session: SessionDep) -> DocumentService:
    return DocumentService(session)


def get_statistics_service(session: SessionDep) -> StatisticsService:
    return StatisticsService(session)


TenderServiceDep = Annotated[TenderService, Depends(get_tender_service)]
SearchServiceDep = Annotated[SearchService, Depends(get_search_service)]
MunicipalityServiceDep = Annotated[MunicipalityService, Depends(get_municipality_service)]
SourceServiceDep = Annotated[SourceService, Depends(get_source_service)]
DocumentServiceDep = Annotated[DocumentService, Depends(get_document_service)]
StatisticsServiceDep = Annotated[StatisticsService, Depends(get_statistics_service)]


# --- Authentication / rate limiting extension points ----------------------


async def optional_api_key(
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> str | None:
    """Extension point for API-key authentication.

    The public read API currently runs unauthenticated. When API keys are
    introduced, this dependency validates the key and returns the client
    identity; every route already depends on it, so no route signatures change.
    """
    return x_api_key


ApiKeyDep = Annotated[str | None, Depends(optional_api_key)]


async def rate_limit_guard(
    request: Request,
    settings: SettingsDep,
    api_key: ApiKeyDep = None,
) -> None:
    """Extension point for rate limiting.

    Disabled by default (``RATE_LIMIT_ENABLED=false``). A Redis-backed limiter
    plugs in here without touching route handlers; the per-client key is
    already resolved (API key first, then client IP).
    """
    if not settings.rate_limit_enabled:
        return
    _client_key = api_key or (request.client.host if request.client else "anonymous")
    # Intentionally a no-op until the limiter backend is implemented; see
    # docs/SECURITY.md ("Rate limiting") for the planned design.
    return
