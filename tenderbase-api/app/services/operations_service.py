"""Operational read models: run reports, failure triage and duplicate review.

TenderBase has no dashboard, so these queries *are* the operator interface
alongside the scripts. They answer the questions an on-call engineer asks:

* ``run_report``   — did a source run, when, how long, what did it produce, why
                     did it fail?
* ``failed_runs``  — the sources that need attention right now.
* ``duplicate_review`` — uncertain matches that were deliberately **not**
                     auto-merged and need a human decision.

All three read persisted evidence only (runs, errors, ``quality_issues``); no
number here is estimated, and unknown values stay ``None``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Select, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.ingestion import IngestionError
from app.db.models.opportunity import ProcurementOpportunity
from app.db.models.source import MunicipalitySource, SourceRun
from app.enums import DataQuality, ErrorStage, HealthStatus, JobStatus, SourceLifecycle
from app.errors import SourceNotFoundError
from app.utils.dates import ensure_utc, utcnow
from app.utils.text import truncate

MAX_ERROR_SAMPLES = 25


@dataclass(slots=True)
class ErrorSample:
    stage: str
    code: str
    message: str
    url: str | None = None
    retryable: bool = False
    occurred_at: datetime | None = None


@dataclass(slots=True)
class RunReport:
    """Everything one run produced, in a single document."""

    run_id: str
    source_id: str
    source_name: str
    source_slug: str
    base_url: str
    connector_key: str | None
    status: str
    started_at: datetime | None
    completed_at: datetime | None
    duration_ms: int | None
    items_found: int
    items_created: int
    items_updated: int
    items_skipped: int
    items_failed: int
    documents_found: int
    uncertain_duplicates: int
    error_count: int
    http_status: int | None
    first_error: str | None
    errors: list[ErrorSample] = field(default_factory=list)
    verdict: str = "UNKNOWN"
    verdict_reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["errors"] = [asdict(error) for error in self.errors]
        return payload


@dataclass(slots=True)
class DuplicateCandidate:
    """An uncertain merge candidate held for review."""

    opportunity_id: str
    title: str
    reference_number: str | None
    source_id: str
    matches: list[dict[str, Any]] = field(default_factory=list)


class OperationsService:
    """Read-side operations queries."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # -- run reporting ----------------------------------------------------

    async def latest_run_report(self, source_id: str | UUID) -> RunReport:
        source = await self._source(source_id)
        run = (
            (
                await self.session.execute(
                    select(SourceRun)
                    .where(SourceRun.source_id == source.id)
                    .order_by(SourceRun.started_at.desc().nulls_last(), SourceRun.id.desc())
                    .limit(1)
                )
            )
            .scalars()
            .first()
        )
        if run is None:
            return self._never_run(source)
        return await self._build_report(source, run)

    async def run_history(self, source_id: str | UUID, *, limit: int = 10) -> list[RunReport]:
        source = await self._source(source_id)
        runs = (
            (
                await self.session.execute(
                    select(SourceRun)
                    .where(SourceRun.source_id == source.id)
                    .order_by(SourceRun.started_at.desc().nulls_last(), SourceRun.id.desc())
                    .limit(max(1, min(limit, 100)))
                )
            )
            .scalars()
            .all()
        )
        return [await self._build_report(source, run) for run in runs]

    async def failed_runs(self, *, limit: int = 25) -> list[RunReport]:
        """Most recent failed runs across all sources, newest first."""
        stmt = (
            select(SourceRun, MunicipalitySource)
            .join(MunicipalitySource, MunicipalitySource.id == SourceRun.source_id)
            .where(SourceRun.status == str(JobStatus.FAILED))
            .order_by(SourceRun.started_at.desc().nulls_last())
            .limit(max(1, min(limit, 200)))
        )
        reports: list[RunReport] = []
        for run, source in (await self.session.execute(stmt)).all():
            reports.append(await self._build_report(source, run))
        return reports

    async def unhealthy_sources(self) -> list[dict[str, Any]]:
        """Sources whose health or lifecycle says "a human should look"."""
        stmt = (
            select(MunicipalitySource)
            .where(
                or_(
                    MunicipalitySource.consecutive_failures >= 1,
                    MunicipalitySource.health_status.in_(
                        [str(HealthStatus.FAILING), str(HealthStatus.OFFLINE)]
                    ),
                    MunicipalitySource.lifecycle_status.in_(
                        [str(SourceLifecycle.DEGRADED), str(SourceLifecycle.PAUSED)]
                    ),
                )
            )
            .order_by(
                MunicipalitySource.consecutive_failures.desc(),
                MunicipalitySource.last_failure_at.desc().nulls_last(),
            )
            .limit(100)
        )
        rows = (await self.session.execute(stmt)).scalars().all()
        return [
            {
                "id": str(source.id),
                "name": source.name,
                "slug": source.slug,
                "connector_key": source.connector_key,
                "lifecycle_status": source.lifecycle_status,
                "health_status": source.health_status,
                "verification_status": source.verification_status,
                "consecutive_failures": source.consecutive_failures,
                "last_run_at": _iso(source.last_run_at),
                "last_success_at": _iso(source.last_success_at),
                "last_failure_at": _iso(source.last_failure_at),
                "last_http_status": source.last_http_status,
            }
            for source in rows
        ]

    # -- duplicate review -------------------------------------------------

    async def duplicate_review_queue(self, *, limit: int = 50) -> list[DuplicateCandidate]:
        """Records carrying an unresolved ``duplicate_review`` note.

        The deduplicator never merges uncertain matches; it writes the candidate
        into ``quality_issues['duplicate_review']``. This surfaces those records
        so a human can merge, split or confirm them.
        """
        stmt = self._duplicate_candidates_stmt().limit(max(1, min(limit, 200)))
        rows = (await self.session.execute(stmt)).scalars().all()
        candidates: list[DuplicateCandidate] = []
        for opportunity in rows:
            review = (opportunity.quality_issues or {}).get("duplicate_review")
            if not isinstance(review, dict):
                continue
            match = {
                "existing_id": review.get("existing_id"),
                "layer": review.get("layer"),
                "confidence": review.get("confidence"),
                "reason": review.get("reason"),
            }
            if existing_id := review.get("existing_id"):
                match["existing_title"] = await self._title_for(existing_id)
            candidates.append(
                DuplicateCandidate(
                    opportunity_id=str(opportunity.id),
                    title=truncate(opportunity.title, 160),
                    reference_number=opportunity.reference_number,
                    source_id=str(opportunity.source_id),
                    matches=[match],
                )
            )
        return candidates

    def _duplicate_candidates_stmt(self) -> Select[tuple[ProcurementOpportunity]]:
        return (
            select(ProcurementOpportunity)
            .where(ProcurementOpportunity.data_quality == str(DataQuality.NEEDS_REVIEW))
            .order_by(ProcurementOpportunity.last_seen_at.desc())
        )

    async def _title_for(self, opportunity_id: str) -> str | None:
        try:
            identifier = UUID(str(opportunity_id))
        except ValueError:
            return None
        return (
            await self.session.execute(
                select(ProcurementOpportunity.title).where(ProcurementOpportunity.id == identifier)
            )
        ).scalar_one_or_none()

    # -- internals --------------------------------------------------------

    async def _source(self, source_id: str | UUID) -> MunicipalitySource:
        try:
            identifier = UUID(str(source_id))
        except ValueError as exc:
            from app.errors import ValidationError

            raise ValidationError("source_id must be a UUID", code="INVALID_ID") from exc
        source = (
            (
                await self.session.execute(
                    select(MunicipalitySource).where(MunicipalitySource.id == identifier)
                )
            )
            .scalars()
            .first()
        )
        if source is None:
            raise SourceNotFoundError(details={"source_id": str(identifier)})
        return source

    def _never_run(self, source: MunicipalitySource) -> RunReport:
        return RunReport(
            run_id="",
            source_id=str(source.id),
            source_name=source.name,
            source_slug=source.slug,
            base_url=source.base_url,
            connector_key=source.connector_key,
            status=str(JobStatus.QUEUED),
            started_at=None,
            completed_at=None,
            duration_ms=None,
            items_found=0,
            items_created=0,
            items_updated=0,
            items_skipped=0,
            items_failed=0,
            documents_found=0,
            uncertain_duplicates=0,
            error_count=0,
            http_status=None,
            first_error=None,
            verdict="NEVER_RUN",
            verdict_reason="This source has no recorded run yet.",
        )

    async def _build_report(self, source: MunicipalitySource, run: SourceRun) -> RunReport:
        errors = (
            (
                await self.session.execute(
                    select(IngestionError)
                    .where(IngestionError.source_run_id == run.id)
                    .order_by(IngestionError.created_at.desc())
                    .limit(MAX_ERROR_SAMPLES)
                )
            )
            .scalars()
            .all()
        )
        samples = [
            ErrorSample(
                stage=str(error.stage),
                code=error.error_code,
                message=truncate(error.message, 400),
                url=error.url,
                retryable=bool(error.retryable),
                occurred_at=error.created_at,
            )
            for error in errors
        ]
        verdict, reason = self._verdict(run, samples)
        stats = run.stats or {}
        return RunReport(
            run_id=str(run.id),
            source_id=str(source.id),
            source_name=source.name,
            source_slug=source.slug,
            base_url=source.base_url,
            connector_key=source.connector_key,
            status=str(run.status),
            started_at=run.started_at,
            completed_at=run.completed_at,
            duration_ms=run.duration_ms,
            items_found=run.items_found,
            items_created=run.items_created,
            items_updated=run.items_updated,
            items_skipped=run.items_skipped,
            items_failed=run.items_failed,
            documents_found=run.documents_found,
            uncertain_duplicates=int(stats.get("uncertain_duplicates", 0) or 0),
            error_count=int(stats.get("error_count", len(samples)) or 0),
            http_status=run.http_status,
            first_error=truncate(run.error_message, 400) if run.error_message else None,
            errors=samples,
            verdict=verdict,
            verdict_reason=reason,
        )

    @staticmethod
    def _verdict(run: SourceRun, errors: list[ErrorSample]) -> tuple[str, str | None]:
        """Classify a run the way an operator reads it, not the way it was stored."""
        if run.status == str(JobStatus.RUNNING):
            return "RUNNING", "The run has not finished."
        if run.items_found == 0:
            fatal = [
                e for e in errors if e.stage in (str(ErrorStage.DISCOVERY), str(ErrorStage.FETCH))
            ]
            if fatal:
                return "UNREACHABLE", f"{fatal[0].code}: {fatal[0].message}"
            return "EMPTY", "The source answered but published no items."
        if run.status == str(JobStatus.FAILED):
            if run.items_created or run.items_updated:
                return "PARTIAL", (
                    f"{run.items_created + run.items_updated} record(s) persisted "
                    f"before the failure; "
                    f"{run.items_failed} item(s) failed."
                )
            head = errors[0] if errors else None
            return "FAILED", (f"{head.code}: {head.message}" if head else run.error_message)
        if run.items_failed or run.items_skipped:
            return "COMPLETED_WITH_ISSUES", (
                f"{run.items_failed} failed, {run.items_skipped} rejected by validation."
            )
        return "HEALTHY", None


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return ensure_utc(value, assume_timezone="UTC").isoformat()


def now_utc() -> datetime:
    return utcnow()
