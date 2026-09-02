"""Statistics endpoint."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.dependencies import MetaDep, StatisticsServiceDep
from app.schemas.category import StatisticsResponse
from app.schemas.common import DataResponse

router = APIRouter(tags=["statistics"])


@router.get(
    "/statistics",
    response_model=DataResponse[StatisticsResponse],
    summary="Platform statistics",
    description=(
        "Aggregate counts computed from **real ingested data only** — the "
        "platform never reports fabricated coverage figures. Records flagged as "
        "development fixtures are excluded from the headline totals and "
        "reported separately as `test_fixture_opportunities`.\n\n"
        "Results are cached briefly server-side; heavier aggregation will move "
        "to materialised views as the dataset grows."
    ),
)
async def statistics(
    service: StatisticsServiceDep, meta: MetaDep
) -> DataResponse[StatisticsResponse]:
    payload = await service.get_statistics()
    return DataResponse[StatisticsResponse](data=payload, meta=meta)
