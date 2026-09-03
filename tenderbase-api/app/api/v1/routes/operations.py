"""Operational endpoints: run reports, failure triage and duplicate review.

Read-only by design. Every *mutation* an operator can make to a source
(verifying, pausing, activating, running an ingestion) is performed through the
operator scripts, which run with database credentials on a machine an operator
controls. Keeping the public API free of write paths removes an entire class of
abuse (any key holder triggering server-side fetches, for example) while still
letting a consumer build monitoring on top of the same data.

See ``docs/PRODUCTION_RUNBOOK.md`` for the script equivalents.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Query

from app.api.dependencies import MetaDep, PaginationDep, SessionDep
from app.api.v1.routes.operations_models import (
    DuplicateCandidate,
    RunReportRead,
    SourceHealthSnapshot,
)
from app.schemas.common import DataResponse, ErrorResponse, ListResponse, PaginationMeta
from app.services.operations_service import OperationsService
from app.services.verification_service import SourceVerificationService

router = APIRouter(prefix="/operations", tags=["operations"])

NOT_FOUND = {404: {"model": ErrorResponse, "description": "Source not found"}}


def _service(session: Annotated[object, Depends]) -> OperationsService:  # pragma: no cover
    return OperationsService(session)  # type: ignore[arg-type]


@router.get(
    "/sources/{source_id}/report",
    response_model=DataResponse[RunReportRead],
    responses=NOT_FOUND,
    summary="Latest ingestion run report for a source",
    description=(
        "The operational answer to: *did this source run, when, for how long, "
        "how much did it produce, and why did it fail?* Includes a verdict "
        "classification (`HEALTHY`, `COMPLETED_WITH_ISSUES`, `PARTIAL`, `EMPTY`, "
        "`UNREACHABLE`, `FAILED`, `NEVER_RUN`) derived only from recorded "
        "evidence — never from an assumption that a 200 response means data "
        "was collected."
    ),
)
async def source_run_report(
    session: SessionDep,
    meta: MetaDep,
    source_id: Annotated[UUID, Path()],
) -> DataResponse[RunReportRead]:
    service = OperationsService(session)
    report = await service.latest_run_report(source_id)
    return DataResponse[RunReportRead](data=RunReportRead(**report.as_dict()), meta=meta)


@router.get(
    "/sources/{source_id}/history",
    response_model=ListResponse[RunReportRead],
    responses=NOT_FOUND,
    summary="Recent run reports for a source",
)
async def source_run_history(
    session: SessionDep,
    meta: MetaDep,
    source_id: Annotated[UUID, Path()],
    limit: Annotated[int, Query(ge=1, le=100)] = 10,
) -> ListResponse[RunReportRead]:
    service = OperationsService(session)
    reports = await service.run_history(source_id, limit=limit)
    payload = [RunReportRead(**report.as_dict()) for report in reports]
    return ListResponse[RunReportRead](
        data=payload,
        pagination=PaginationMeta.build(
            page=1, page_size=max(len(payload), 1), total_items=len(payload)
        ),
        meta=meta,
    )


@router.get(
    "/runs/failed",
    response_model=ListResponse[RunReportRead],
    summary="Most recent failed runs across all sources",
)
async def failed_runs(
    session: SessionDep,
    meta: MetaDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 25,
) -> ListResponse[RunReportRead]:
    service = OperationsService(session)
    reports = await service.failed_runs(limit=limit)
    payload = [RunReportRead(**report.as_dict()) for report in reports]
    return ListResponse[RunReportRead](
        data=payload,
        pagination=PaginationMeta.build(
            page=1, page_size=max(len(payload), 1), total_items=len(payload)
        ),
        meta=meta,
    )


@router.get(
    "/sources/unhealthy",
    response_model=ListResponse[SourceHealthSnapshot],
    summary="Sources that need operator attention",
    description=(
        "Any source with recorded failures, a FAILING/OFFLINE health status, or "
        "a DEGRADED/PAUSED lifecycle state. Ordered by consecutive failures so "
        "the worst offender is first."
    ),
)
async def unhealthy_sources(
    session: SessionDep,
    meta: MetaDep,
) -> ListResponse[SourceHealthSnapshot]:
    service = OperationsService(session)
    rows = await service.unhealthy_sources()
    payload = [SourceHealthSnapshot.model_validate(row) for row in rows]
    return ListResponse[SourceHealthSnapshot](
        data=payload,
        pagination=PaginationMeta.build(
            page=1, page_size=max(len(payload), 1), total_items=len(payload)
        ),
        meta=meta,
    )


@router.get(
    "/duplicates/review",
    response_model=ListResponse[DuplicateCandidate],
    summary="Uncertain duplicate matches awaiting human review",
    description=(
        "TenderBase **never auto-merges** an uncertain duplicate. Those records "
        "are persisted separately and listed here with the candidate they "
        "matched, the layer that matched and the confidence, so a human can "
        "decide. An empty list means nothing is pending — not that dedup "
        "disabled itself."
    ),
)
async def duplicate_review_queue(
    session: SessionDep,
    meta: MetaDep,
    pagination: PaginationDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> ListResponse[DuplicateCandidate]:
    service = OperationsService(session)
    candidates = await service.duplicate_review_queue(limit=limit)
    payload = [DuplicateCandidate.model_validate(item.__dict__) for item in candidates]
    return ListResponse[DuplicateCandidate](
        data=payload,
        pagination=PaginationMeta.build(
            page=1, page_size=max(len(payload), 1), total_items=len(payload)
        ),
        meta=meta,
    )


@router.get(
    "/sources/{source_id}/verification",
    response_model=DataResponse[dict],
    responses=NOT_FOUND,
    summary="Last recorded verification outcome for a source",
    description=(
        "The stored evidence from the last `scripts/verify_source` run: per-check "
        "status and detail. `verification_status=UNVERIFIED` with no report means "
        "the source has never been checked — which is the honest default."
    ),
)
async def last_verification(
    session: SessionDep,
    meta: MetaDep,
    source_id: Annotated[UUID, Path()],
) -> DataResponse[dict]:
    service = SourceVerificationService(session)
    source = await service.get(source_id)
    return DataResponse[dict](
        data={
            "source_id": str(source.id),
            "slug": source.slug,
            "verification_status": source.verification_status,
            "lifecycle_status": source.lifecycle_status,
            "verified_at": source.verified_at.isoformat() if source.verified_at else None,
            "verification_at": (
                source.verification_at.isoformat() if source.verification_at else None
            ),
            "verification_duration_ms": source.verification_duration_ms,
            "verification_http_status": source.verification_http_status,
            "report": source.verification_result,
        },
        meta=meta,
    )
