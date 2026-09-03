"""Shared FastAPI dependencies: sessions, pagination, services and auth hooks."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.query_filters import parse_query_filter
from app.config import Settings, get_settings
from app.db.session import get_session
from app.errors import ValidationError
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
        int | None,
        Query(
            ge=1,
            alias="page_size",
            description="Items per page; 1..MAX_PAGE_SIZE, default DEFAULT_PAGE_SIZE",
        ),
    ] = None,
    settings: SettingsDep = None,  # type: ignore[assignment]
) -> PaginationParams:
    """Validated pagination parameters, bounded by the *application's* settings.

    The ceiling is looked up here rather than written into the query annotation as
    ``le=100``, because `MAX_PAGE_SIZE` is an operator knob: with a constant in the
    annotation, raising the setting did nothing, while *lowering* it was quietly ignored —
    a request for 80 rows against `MAX_PAGE_SIZE=50` was answered with 50, so the
    ``page_size`` the client sent no longer described the page it got. Rejecting with 422
    is the only honest reading of "bounded pagination": the client learns the limit
    instead of receiving a different answer than it asked for.
    """
    cfg = settings or get_settings()
    # A default is never a client error: if an operator configures DEFAULT_PAGE_SIZE above
    # MAX_PAGE_SIZE the API serves the smaller of the two instead of refusing its own default.
    requested = min(cfg.default_page_size, cfg.max_page_size) if page_size is None else page_size
    if requested > cfg.max_page_size:
        raise ValidationError(
            f"page_size must be at most {cfg.max_page_size} (MAX_PAGE_SIZE)",
            details={
                "errors": [
                    {
                        "field": "query.page_size",
                        "message": f"Input should be less than or equal to {cfg.max_page_size}",
                        "type": "less_than_equal",
                    }
                ]
            },
        )
    return parse_query_filter(PaginationParams, page=page, page_size=requested)


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


# --- Authentication / rate limiting ---------------------------------------
#
# The real implementations live in :mod:`app.api.auth` (key verification,
# scope checks, rate limiting) and are attached to the protected routers in
# :mod:`app.api.v1.router`. They are re-exported here so route modules have a
# single import site for everything they need from the API layer.

from app.api.auth import Principal, api_access  # noqa: E402

ApiKeyDep = Annotated[Principal, Depends(api_access)]
