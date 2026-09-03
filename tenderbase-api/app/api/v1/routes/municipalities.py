"""Municipality endpoints."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query

from app.api.dependencies import MetaDep, MunicipalityServiceDep, PaginationDep, TenderServiceDep
from app.api.v1.routes.tenders import TenderFilterDep
from app.schemas.common import DataResponse, ErrorResponse, ListResponse, PaginationMeta
from app.schemas.municipality import MunicipalityFilter, MunicipalityRead
from app.schemas.tender import TenderRead

router = APIRouter(prefix="/municipalities", tags=["municipalities"])

NOT_FOUND = {404: {"model": ErrorResponse, "description": "Municipality not found"}}


def municipality_filters(
    province: Annotated[str | None, Query(description="Province name, code or slug")] = None,
    type: Annotated[  # noqa: A002
        str | None, Query(description="METROPOLITAN | DISTRICT | LOCAL")
    ] = None,
    q: Annotated[str | None, Query(max_length=200, description="Name contains")] = None,
    active: Annotated[bool | None, Query()] = None,
) -> MunicipalityFilter:
    return MunicipalityFilter(province=province, type=type, q=q, active=active)


@router.get(
    "",
    response_model=ListResponse[MunicipalityRead],
    summary="List municipalities",
    description=(
        "Paginated list of South African municipalities (metropolitan, district "
        "and local), each linked to its province and district. Geographic data "
        "is imported from an authoritative dataset — see `data_source` on each "
        "record for provenance."
    ),
)
async def list_municipalities(
    service: MunicipalityServiceDep,
    pagination: PaginationDep,
    meta: MetaDep,
    filters: Annotated[MunicipalityFilter, Depends(municipality_filters)],
) -> ListResponse[MunicipalityRead]:
    items, total = await service.list_municipalities(filters, pagination)
    return ListResponse[MunicipalityRead](
        data=[MunicipalityRead.model_validate(item) for item in items],
        pagination=PaginationMeta.build(
            page=pagination.page, page_size=pagination.page_size, total_items=total
        ),
        meta=meta,
    )


@router.get(
    "/{identifier}",
    response_model=DataResponse[MunicipalityRead],
    responses=NOT_FOUND,
    summary="Get a municipality",
    description="Look up a municipality by UUID, official code (e.g. `ETH`) or slug.",
)
async def get_municipality(
    service: MunicipalityServiceDep,
    meta: MetaDep,
    identifier: Annotated[str, Path(description="UUID, municipality code or slug")],
) -> DataResponse[MunicipalityRead]:
    municipality = await service.get_municipality(identifier)
    return DataResponse[MunicipalityRead](
        data=MunicipalityRead.model_validate(municipality), meta=meta
    )


@router.get(
    "/{identifier}/tenders",
    response_model=ListResponse[TenderRead],
    responses=NOT_FOUND,
    summary="List a municipality's procurement opportunities",
    description=(
        "Opportunities attributed to one municipality. Accepts the same filters "
        "as `/tenders`; the municipality is taken from the path."
    ),
)
async def list_municipality_tenders(
    municipality_service: MunicipalityServiceDep,
    tender_service: TenderServiceDep,
    pagination: PaginationDep,
    meta: MetaDep,
    filters: TenderFilterDep,
    identifier: Annotated[str, Path(description="UUID, municipality code or slug")],
) -> ListResponse[TenderRead]:
    municipality = await municipality_service.get_municipality(identifier)
    filters.municipality_id = municipality.id
    filters.municipality = None
    page = await tender_service.list_tenders(filters, pagination)
    return ListResponse[TenderRead](
        data=[TenderRead.model_validate(item) for item in page.items],
        pagination=PaginationMeta.build(
            page=pagination.page, page_size=pagination.page_size, total_items=page.total
        ),
        meta=meta,
    )
