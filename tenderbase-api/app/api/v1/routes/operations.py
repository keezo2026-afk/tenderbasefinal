"""Operational endpoints: run reports, failure triage, duplicate review, recovery.

Read-only by design — with exactly one deliberate exception. Every *mutation* an
operator can make to a source (verifying, pausing, activating, running an ingestion)
is performed through the operator scripts, which run with database credentials on a
machine an operator controls. Keeping the public API free of write paths removes an
entire class of abuse (any key holder triggering server-side fetches, for example)
while still letting a consumer build monitoring on top of the same data.

``POST /operations/reconcile`` is that exception, and it is not a data path: it runs
no crawl and touches no opportunity. It exists because the worker's own recovery pass
has an interval, and the moment an operator is reading this endpoint is the moment they
want the interval bypassed. It is therefore scoped to ``admin`` (not ``read:sources``,
which every read-only key holds), it is idempotent, and ``dry_run`` answers "what would
you do" without writing anything.

See ``docs/PRODUCTION_RUNBOOK.md`` for the script equivalents.
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Query

from app.api.auth import AdminDep
from app.api.dependencies import MetaDep, PaginationDep, SessionDep
from app.api.v1.routes.operations_models import (
    DuplicateCandidate,
    RecoveryReportRead,
    RunReportRead,
    SourceFreshnessRead,
    SourceHealthSnapshot,
)
from app.logging import get_logger
from app.schemas.common import DataResponse, ErrorResponse, ListResponse, PaginationMeta
from app.services.job_recovery import reconcile, source_freshness
from app.services.operations_service import OperationsService
from app.services.verification_service import SourceVerificationService

logger = get_logger("tenderbase.api.operations")

router = APIRouter(prefix="/operations", tags=["operations"])

NOT_FOUND: dict[int | str, dict[str, Any]] = {
    404: {"model": ErrorResponse, "description": "Source not found"}
}


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


@router.get(
    "/sources/freshness",
    response_model=ListResponse[SourceFreshnessRead],
    summary="How out of date each source's data is",
    description=(
        "Sources ordered by how badly they have missed their crawl schedule, "
        "classified against ``FRESHNESS_AGING_HOURS`` / ``FRESHNESS_STALE_HOURS``. "
        "`NEVER_RUN` means no successful run has ever been recorded — which for a "
        "newly registered source is normal and for one that used to work is not. A "
        "past-due `claim_expires_at` alongside no live job is a stale lease; the "
        "reconciliation pass clears it."
    ),
)
async def sources_freshness(
    session: SessionDep,
    meta: MetaDep,
    state: Annotated[
        str | None,
        Query(
            pattern="^(FRESH|AGING|STALE|NEVER_RUN|PAUSED|NOT_ACTIVE)$",
            description="Only sources in this freshness state.",
        ),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=2000)] = 500,
) -> ListResponse[SourceFreshnessRead]:
    rows, _summary = await source_freshness(session, limit=limit)
    if state is not None:
        rows = [row for row in rows if row["freshness_state"] == state]
    payload = [SourceFreshnessRead(**row) for row in rows]
    return ListResponse[SourceFreshnessRead](
        data=payload,
        pagination=PaginationMeta.build(
            page=1, page_size=max(len(payload), 1), total_items=len(payload)
        ),
        meta=meta,
    )


@router.post(
    "/reconcile",
    response_model=DataResponse[RecoveryReportRead],
    summary="Reconcile ingestion jobs, source runs and scheduler leases now",
    description=(
        "Compares ``ingestion_jobs`` and ``source_runs`` against reality and repairs what "
        "the worker lost: jobs queued but never dispatched, jobs ``RUNNING`` past the "
        "worker timeout, ``RETRYING`` jobs with no deferred execution left, runs stuck "
        "open, and expired source claim leases. The same pass runs on a cron inside every "
        "worker every ``JOB_RECONCILIATION_INTERVAL_SECONDS``; this endpoint is for when "
        "an operator wants it *now*, and for reading what it would do "
        "(``dry_run=true`` writes nothing).\n\n"
        "**Idempotent.** Every repair moves the row out of the state that selected it, so "
        "the second call in a row reports ``actions_count=0``. Requires the ``admin`` "
        "scope: a read-only key cannot trigger this, and re-dispatching queued work is a "
        "write to the queue."
    ),
)
async def reconcile_now(
    session: SessionDep,
    meta: MetaDep,
    _principal: AdminDep,
    dry_run: Annotated[
        bool, Query(description="Report the repairs without applying them.")
    ] = False,
) -> DataResponse[RecoveryReportRead]:
    from app.workers.queue import get_queue

    # Resolved *before* the pass runs: ``reconcile`` commits, and the request session's
    # objects expire with it, so reading the principal afterwards would trigger a lazy
    # load for a log line — and in a detached-by-then instance, an error.
    actor = _principal.display_id

    queue = None
    try:
        queue = get_queue()
    except Exception as exc:  # noqa: BLE001 - the queue is optional for recovery
        logger.warning("operations.reconcile_queue_unavailable", error=str(exc))

    report = await reconcile(session, queue=queue, dry_run=dry_run)
    # Who asked, and what changed: this is the one write path in the operations API,
    # so its audit trail is a log line, not a row.
    logger.info(
        "operations.reconcile",
        dry_run=report.dry_run,
        actions=report.counts,
        principal=actor,
    )
    return DataResponse[RecoveryReportRead](data=RecoveryReportRead(**report.as_dict()), meta=meta)
