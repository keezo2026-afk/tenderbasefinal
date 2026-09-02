"""Tender endpoints: listing, detail, documents, events and versions."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Query

from app.api.dependencies import MetaDep, PaginationDep, TenderServiceDep
from app.schemas.common import DataResponse, ErrorResponse, ListResponse, PaginationMeta
from app.schemas.document import DocumentRead
from app.schemas.event import EventRead, VersionRead
from app.schemas.tender import TenderDetail, TenderFilter, TenderRead

router = APIRouter(prefix="/tenders", tags=["tenders"])

NOT_FOUND = {404: {"model": ErrorResponse, "description": "Tender not found"}}


def tender_filters(
    province: Annotated[str | None, Query(description="Province name, code or slug")] = None,
    district: Annotated[str | None, Query(description="District name, code or slug")] = None,
    municipality: Annotated[
        str | None, Query(description="Municipality name, code or slug")
    ] = None,
    municipality_id: Annotated[UUID | None, Query()] = None,
    source_id: Annotated[UUID | None, Query()] = None,
    type: Annotated[  # noqa: A002 - the public query name is 'type'
        str | None, Query(description="Procurement type, e.g. RFQ, TENDER, RFP")
    ] = None,
    status: Annotated[str | None, Query(description="Status, e.g. OPEN, CLOSED, AWARDED")] = None,
    category: Annotated[str | None, Query(description="Category slug")] = None,
    reference_number: Annotated[str | None, Query(max_length=200)] = None,
    published_after: Annotated[str | None, Query(description="ISO date or datetime")] = None,
    published_before: Annotated[str | None, Query(description="ISO date or datetime")] = None,
    closing_after: Annotated[str | None, Query(description="ISO date or datetime")] = None,
    closing_before: Annotated[str | None, Query(description="ISO date or datetime")] = None,
    min_value: Annotated[float | None, Query(ge=0)] = None,
    max_value: Annotated[float | None, Query(ge=0)] = None,
    data_quality: Annotated[
        str | None, Query(description="VALID, INCOMPLETE, NEEDS_REVIEW or INVALID")
    ] = None,
    include_test_fixtures: Annotated[
        bool, Query(description="Include clearly-marked development fixture records")
    ] = False,
    q: Annotated[str | None, Query(max_length=300, description="Free-text query")] = None,
    sort: Annotated[
        str,
        Query(
            description="published_at | closing_at | created_at | last_seen_at | title | relevance "
            "(prefix with '-' for descending)"
        ),
    ] = "-published_at",
) -> TenderFilter:
    """Build and validate the typed tender filter from query parameters."""
    return TenderFilter(
        province=province,
        district=district,
        municipality=municipality,
        municipality_id=municipality_id,
        source_id=source_id,
        type=type,
        status=status,
        category=category,
        reference_number=reference_number,
        published_after=published_after,
        published_before=published_before,
        closing_after=closing_after,
        closing_before=closing_before,
        min_value=min_value,
        max_value=max_value,
        data_quality=data_quality,
        include_test_fixtures=include_test_fixtures,
        q=q,
        sort=sort,
    )


TenderFilterDep = Annotated[TenderFilter, Depends(tender_filters)]


@router.get(
    "",
    response_model=ListResponse[TenderRead],
    summary="List procurement opportunities",
    description=(
        "Returns a paginated, filterable list of normalized procurement "
        "opportunities.\n\n"
        "**Filtering** — combine any of `province`, `district`, `municipality`, "
        "`type`, `status`, `category`, date ranges and value ranges.\n\n"
        "**Pagination** — deterministic offset pagination via `page` and "
        "`page_size` (server-enforced maximum). Results are always ordered with "
        "a stable tiebreaker so pages never overlap.\n\n"
        "**Fixtures** — development/test fixture records are excluded unless "
        "`include_test_fixtures=true`."
    ),
)
async def list_tenders(
    service: TenderServiceDep,
    filters: TenderFilterDep,
    pagination: PaginationDep,
    meta: MetaDep,
) -> ListResponse[TenderRead]:
    page = await service.list_tenders(filters, pagination)
    return ListResponse[TenderRead](
        data=[TenderRead.model_validate(item) for item in page.items],
        pagination=PaginationMeta.build(
            page=pagination.page, page_size=pagination.page_size, total_items=page.total
        ),
        meta=meta,
    )


@router.get(
    "/{tender_id}",
    response_model=DataResponse[TenderDetail],
    responses=NOT_FOUND,
    summary="Get a procurement opportunity",
    description=(
        "Returns the full canonical record: content, dates, submission and "
        "briefing details, contact, documents, provenance (source URL, content "
        "hash) and data-quality assessment."
    ),
)
async def get_tender(
    service: TenderServiceDep,
    meta: MetaDep,
    tender_id: Annotated[UUID, Path(description="Opportunity UUID")],
) -> DataResponse[TenderDetail]:
    opportunity = await service.get_tender(tender_id)
    detail = TenderDetail.model_validate(opportunity)
    detail.documents = [DocumentRead.model_validate(doc) for doc in opportunity.documents]
    detail.categories = [link.category.slug for link in opportunity.categories if link.category]
    return DataResponse[TenderDetail](data=detail, meta=meta)


@router.get(
    "/{tender_id}/documents",
    response_model=ListResponse[DocumentRead],
    responses=NOT_FOUND,
    summary="List documents for an opportunity",
    description=(
        "Returns document metadata attached to an opportunity. `sha256` is "
        "populated once the file has been downloaded; `is_downloaded=false` "
        "means only the link has been discovered so far."
    ),
)
async def list_tender_documents(
    service: TenderServiceDep,
    meta: MetaDep,
    tender_id: Annotated[UUID, Path(description="Opportunity UUID")],
) -> ListResponse[DocumentRead]:
    documents = await service.get_documents(tender_id)
    return ListResponse[DocumentRead](
        data=[DocumentRead.model_validate(doc) for doc in documents],
        pagination=PaginationMeta.build(
            page=1, page_size=max(len(documents), 1), total_items=len(documents)
        ),
        meta=meta,
    )


@router.get(
    "/{tender_id}/events",
    response_model=ListResponse[EventRead],
    responses=NOT_FOUND,
    summary="List change events for an opportunity",
    description=(
        "Chronological change feed: deadline changes, briefing changes, status "
        "changes, documents added/removed and award postings. Newest first."
    ),
)
async def list_tender_events(
    service: TenderServiceDep,
    pagination: PaginationDep,
    meta: MetaDep,
    tender_id: Annotated[UUID, Path(description="Opportunity UUID")],
) -> ListResponse[EventRead]:
    events, total = await service.get_events(tender_id, pagination)
    return ListResponse[EventRead](
        data=[_event_to_schema(event) for event in events],
        pagination=PaginationMeta.build(
            page=pagination.page, page_size=pagination.page_size, total_items=total
        ),
        meta=meta,
    )


@router.get(
    "/{tender_id}/versions",
    response_model=ListResponse[VersionRead],
    responses=NOT_FOUND,
    summary="List historical versions of an opportunity",
    description=(
        "Immutable version history. Each entry records the content hash and the "
        "field-level diff against the previous version."
    ),
)
async def list_tender_versions(
    service: TenderServiceDep,
    pagination: PaginationDep,
    meta: MetaDep,
    tender_id: Annotated[UUID, Path(description="Opportunity UUID")],
) -> ListResponse[VersionRead]:
    versions, total = await service.get_versions(tender_id, pagination)
    return ListResponse[VersionRead](
        data=[VersionRead.model_validate(version) for version in versions],
        pagination=PaginationMeta.build(
            page=pagination.page, page_size=pagination.page_size, total_items=total
        ),
        meta=meta,
    )


def _event_to_schema(event) -> EventRead:  # noqa: ANN001
    """Unwrap the stored ``{"value": ...}`` envelope for the public schema."""
    payload = EventRead.model_validate(event)
    if isinstance(event.previous_value, dict):
        payload.previous_value = event.previous_value.get("value")
    if isinstance(event.new_value, dict):
        payload.new_value = event.new_value.get("value")
    return payload
