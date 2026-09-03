"""Source registry endpoints."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Query

from app.api.dependencies import MetaDep, PaginationDep, SourceServiceDep
from app.connectors.registry import list_connectors
from app.schemas.common import DataResponse, ErrorResponse, ListResponse, PaginationMeta
from app.schemas.source import (
    ConnectorRead,
    SourceFilter,
    SourceHealth,
    SourceRead,
    SourceRunRead,
)

router = APIRouter(prefix="/sources", tags=["sources"])

NOT_FOUND = {404: {"model": ErrorResponse, "description": "Source not found"}}


def source_filters(
    source_type: Annotated[str | None, Query(description="e.g. MUNICIPAL_RFQ")] = None,
    connector_type: Annotated[
        str | None, Query(description="HTTP | HTML | WORDPRESS | PDF | BROWSER | CUSTOM")
    ] = None,
    health_status: Annotated[
        str | None, Query(description="HEALTHY | DEGRADED | FAILING | OFFLINE | UNKNOWN")
    ] = None,
    province: Annotated[str | None, Query()] = None,
    municipality_id: Annotated[UUID | None, Query()] = None,
    active: Annotated[bool | None, Query()] = None,
    q: Annotated[str | None, Query(max_length=200)] = None,
) -> SourceFilter:
    return SourceFilter(
        source_type=source_type,
        connector_type=connector_type,
        health_status=health_status,
        province=province,
        municipality_id=municipality_id,
        active=active,
        q=q,
    )


def _to_schema(source) -> SourceRead:  # noqa: ANN001
    payload = SourceRead.model_validate(source)
    payload.health = SourceHealth.model_validate(source)
    return payload


@router.get(
    "",
    response_model=ListResponse[SourceRead],
    summary="List procurement sources",
    description=(
        "The source registry: every configured place TenderBase collects data "
        "from, with its connector type, crawl policy and operational health.\n\n"
        "Sources are registered by operators from verified information — the "
        "registry never contains invented URLs."
    ),
)
async def list_sources(
    service: SourceServiceDep,
    pagination: PaginationDep,
    meta: MetaDep,
    filters: Annotated[SourceFilter, Depends(source_filters)],
) -> ListResponse[SourceRead]:
    items, total = await service.list_sources(filters, pagination)
    return ListResponse[SourceRead](
        data=[_to_schema(item) for item in items],
        pagination=PaginationMeta.build(
            page=pagination.page, page_size=pagination.page_size, total_items=total
        ),
        meta=meta,
    )


@router.get(
    "/connectors",
    response_model=ListResponse[ConnectorRead],
    summary="List available connectors",
    description=(
        "Connector implementations registered in this build, with the "
        "configuration keys each accepts. Use this to discover what "
        "`connector_key` values a source may reference."
    ),
)
async def list_available_connectors(meta: MetaDep) -> ListResponse[ConnectorRead]:
    connectors = list_connectors()
    return ListResponse[ConnectorRead](
        data=[ConnectorRead.model_validate(item) for item in connectors],
        pagination=PaginationMeta.build(
            page=1, page_size=max(len(connectors), 1), total_items=len(connectors)
        ),
        meta=meta,
    )


@router.get(
    "/{source_id}",
    response_model=DataResponse[SourceRead],
    responses=NOT_FOUND,
    summary="Get a source",
    description="Full source definition including health metrics and verification notes.",
)
async def get_source(
    service: SourceServiceDep,
    meta: MetaDep,
    source_id: Annotated[UUID, Path(description="Source UUID")],
) -> DataResponse[SourceRead]:
    source = await service.get_source(source_id)
    return DataResponse[SourceRead](data=_to_schema(source), meta=meta)


@router.get(
    "/{source_id}/runs",
    response_model=ListResponse[SourceRunRead],
    responses=NOT_FOUND,
    summary="List ingestion runs for a source",
    description=(
        "Execution history for a source: status, timing and per-run counters "
        "(items found/created/updated/skipped/failed). Newest first."
    ),
)
async def list_source_runs(
    service: SourceServiceDep,
    pagination: PaginationDep,
    meta: MetaDep,
    source_id: Annotated[UUID, Path(description="Source UUID")],
) -> ListResponse[SourceRunRead]:
    runs, total = await service.list_runs(source_id, pagination)
    return ListResponse[SourceRunRead](
        data=[SourceRunRead.model_validate(run) for run in runs],
        pagination=PaginationMeta.build(
            page=pagination.page, page_size=pagination.page_size, total_items=total
        ),
        meta=meta,
    )
