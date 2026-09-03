"""Province and district endpoints."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path, Query

from app.api.dependencies import MetaDep, MunicipalityServiceDep
from app.schemas.common import DataResponse, ErrorResponse, ListResponse, PaginationMeta
from app.schemas.municipality import DistrictRead, ProvinceRead

router = APIRouter(prefix="/provinces", tags=["provinces"])

NOT_FOUND = {404: {"model": ErrorResponse, "description": "Province not found"}}


@router.get(
    "",
    response_model=ListResponse[ProvinceRead],
    summary="List provinces",
    description=(
        "All South African provinces held in the reference dataset. Province "
        "codes (e.g. `GT`, `KZN`, `WC`) can be used as filter values on the "
        "tender, municipality and source endpoints."
    ),
)
async def list_provinces(
    service: MunicipalityServiceDep, meta: MetaDep
) -> ListResponse[ProvinceRead]:
    provinces = await service.list_provinces()
    return ListResponse[ProvinceRead](
        data=[ProvinceRead.model_validate(item) for item in provinces],
        pagination=PaginationMeta.build(
            page=1, page_size=max(len(provinces), 1), total_items=len(provinces)
        ),
        meta=meta,
    )


@router.get(
    "/districts",
    response_model=ListResponse[DistrictRead],
    summary="List district municipalities",
    description="District municipalities (category C), optionally filtered by province.",
)
async def list_districts(
    service: MunicipalityServiceDep,
    meta: MetaDep,
    province: Annotated[str | None, Query(description="Province name, code or slug")] = None,
) -> ListResponse[DistrictRead]:
    districts = await service.list_districts(province)
    return ListResponse[DistrictRead](
        data=[DistrictRead.model_validate(item) for item in districts],
        pagination=PaginationMeta.build(
            page=1, page_size=max(len(districts), 1), total_items=len(districts)
        ),
        meta=meta,
    )


@router.get(
    "/{identifier}",
    response_model=DataResponse[ProvinceRead],
    responses=NOT_FOUND,
    summary="Get a province",
    description="Look up a province by code, slug or name.",
)
async def get_province(
    service: MunicipalityServiceDep,
    meta: MetaDep,
    identifier: Annotated[str, Path(description="Province code, slug or name")],
) -> DataResponse[ProvinceRead]:
    province = await service.get_province(identifier)
    return DataResponse[ProvinceRead](data=ProvinceRead.model_validate(province), meta=meta)
