"""The ingestion pipeline.

    SOURCE → DISCOVERY → FETCH → PARSE → VALIDATE → NORMALIZE →
    DEDUPLICATE → VERSION → DOCUMENT DISCOVERY → PERSIST

Every stage is observable: a :class:`SourceRun` row records counters and
timing, per-item failures become ``ingestion_errors`` rows, and source health
is updated at the end of the run. One broken source never affects another.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.connectors.base import RawItem, SourceContext
from app.connectors.registry import build_connector
from app.db.models.document import Document
from app.db.models.ingestion import IngestionError, IngestionJob
from app.db.models.opportunity import Contact, ProcurementOpportunity
from app.db.models.source import MunicipalitySource, SourceRun
from app.documents.storage import store_raw_payload
from app.enums import (
    DataQuality,
    ErrorStage,
    EventType,
    HealthStatus,
    JobStatus,
)
from app.errors import TenderBaseError
from app.ingestion.deduplicator import Deduplicator
from app.ingestion.normalizer import Normalizer
from app.ingestion.parser import StageError, parse_source
from app.ingestion.validator import Validator
from app.ingestion.versioning import VersionEngine
from app.logging import get_logger, source_id_ctx
from app.schemas.tender import NormalizedOpportunity
from app.utils.dates import utcnow
from app.utils.hashing import contact_fingerprint

logger = get_logger("tenderbase.pipeline")


@dataclass(slots=True)
class RunStats:
    """Counters for one source run."""

    items_found: int = 0
    items_created: int = 0
    items_updated: int = 0
    items_skipped: int = 0
    items_failed: int = 0
    documents_found: int = 0
    uncertain_duplicates: int = 0
    errors: list[StageError] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "items_found": self.items_found,
            "items_created": self.items_created,
            "items_updated": self.items_updated,
            "items_skipped": self.items_skipped,
            "items_failed": self.items_failed,
            "documents_found": self.documents_found,
            "uncertain_duplicates": self.uncertain_duplicates,
            "error_count": len(self.errors),
        }


class IngestionPipeline:
    """Runs one source end to end and persists the results."""

    def __init__(
        self,
        *,
        fetcher: Any,
        settings: Settings | None = None,
        normalizer: Normalizer | None = None,
        validator: Validator | None = None,
        deduplicator: Deduplicator | None = None,
        version_engine: VersionEngine | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.fetcher = fetcher
        self.normalizer = normalizer or Normalizer()
        self.validator = validator or Validator()
        self.deduplicator = deduplicator or Deduplicator()
        self.versions = version_engine or VersionEngine()

    # -- public API -------------------------------------------------------

    async def run_source(
        self,
        session: AsyncSession,
        source: MunicipalitySource,
        *,
        job: IngestionJob | None = None,
        commit: bool = True,
    ) -> SourceRun:
        """Execute the full pipeline for one source.

        Always returns a persisted :class:`SourceRun`, whether it succeeded or
        failed — failures are data, not exceptions, at this level.
        """
        token = source_id_ctx.set(str(source.id))
        started = utcnow()
        run = SourceRun(
            source_id=source.id,
            job_id=job.id if job else None,
            status=str(JobStatus.RUNNING),
            started_at=started,
        )
        session.add(run)
        await session.flush()

        stats = RunStats()
        context = SourceContext.from_model(source)

        try:
            connector = build_connector(
                source.connector_key, source.connector_type, fetcher=self.fetcher
            )
            logger.info(
                "pipeline.start",
                source_id=str(source.id),
                connector=connector.key,
                job_id=str(job.id) if job else None,
            )
            async for item in parse_source(connector, context, errors=stats.errors):
                stats.items_found += 1
                try:
                    await self._process_item(session, source, context, item, run, stats)
                except Exception as exc:  # noqa: BLE001 - isolate per-item failures
                    stats.items_failed += 1
                    stats.errors.append(
                        StageError.from_exception(
                            exc, stage=ErrorStage.PERSIST, url=item.source_url
                        )
                    )
                    logger.warning(
                        "pipeline.item_failed",
                        source_id=str(source.id),
                        url=item.source_url,
                        error=str(exc),
                    )
        except TenderBaseError as exc:
            stats.errors.append(StageError.from_exception(exc, stage=ErrorStage.DISCOVERY))
            logger.error("pipeline.failed", source_id=str(source.id), error=str(exc))
        except Exception as exc:  # noqa: BLE001 - never let one source kill the worker
            stats.errors.append(StageError.from_exception(exc, stage=ErrorStage.UNKNOWN))
            logger.exception("pipeline.unexpected_failure", source_id=str(source.id))
        finally:
            source_id_ctx.reset(token)

        await self._finalise(session, source, run, stats, started=started, job=job)
        if commit:
            await session.commit()
        return run

    # -- stages -----------------------------------------------------------

    async def _process_item(
        self,
        session: AsyncSession,
        source: MunicipalitySource,
        context: SourceContext,
        item: RawItem,
        run: SourceRun,
        stats: RunStats,
    ) -> None:
        """NORMALIZE → VALIDATE → DEDUPLICATE → VERSION → PERSIST for one item."""
        record = self.normalizer.normalize(
            item,
            context,
            municipality_id=source.municipality_id,
            province_id=source.province_id,
        )
        record = self._offload_raw_payload(record, item)

        validation = self.validator.validate(record)
        record.data_quality = validation.quality
        record.quality_issues = validation.as_dict()
        if not validation.is_persistable:
            stats.items_skipped += 1
            stats.errors.append(
                StageError(
                    stage=ErrorStage.VALIDATE,
                    code="RECORD_INVALID",
                    message=f"Record rejected: {validation.issues}",
                    url=record.source_url,
                    context={"issues": validation.issues},
                )
            )
            return

        duplicate = await self.deduplicator.find_duplicate(session, record)
        if duplicate.decision.name == "UNCERTAIN":
            stats.uncertain_duplicates += 1
            record.quality_issues["duplicate_review"] = {
                "existing_id": str(duplicate.existing_id),
                "layer": duplicate.layer,
                "confidence": duplicate.confidence,
                "reason": duplicate.reason,
            }
            if record.data_quality is DataQuality.VALID:
                record.data_quality = DataQuality.NEEDS_REVIEW

        if duplicate.is_duplicate and duplicate.existing_id is not None:
            existing = await self._load_opportunity(session, duplicate.existing_id)
            if existing is not None:
                updated = await self._update_existing(session, existing, record, run)
                stats.items_updated += int(updated)
                stats.items_skipped += int(not updated)
                stats.documents_found += len(record.documents)
                return

        await self._create_opportunity(session, record, run)
        stats.items_created += 1
        stats.documents_found += len(record.documents)

    def _offload_raw_payload(
        self, record: NormalizedOpportunity, item: RawItem
    ) -> NormalizedOpportunity:
        """Keep small raw payloads inline; move large HTML to the blob store."""
        if item.raw_html and len(item.raw_html) > self.settings.raw_payload_inline_max_bytes:
            try:
                record.raw_payload_key = store_raw_payload(
                    item.raw_html, namespace="html", settings=self.settings
                )
            except Exception as exc:  # noqa: BLE001 - storage must not fail ingestion
                logger.warning("pipeline.raw_payload_store_failed", error=str(exc))
        elif item.raw_html:
            record.raw_payload = {**(record.raw_payload or {}), "html": item.raw_html}
        return record

    async def _load_opportunity(
        self, session: AsyncSession, opportunity_id: UUID
    ) -> ProcurementOpportunity | None:
        stmt = select(ProcurementOpportunity).where(ProcurementOpportunity.id == opportunity_id)
        opportunity = (await session.execute(stmt)).scalars().first()
        if opportunity is not None:
            # Documents are needed by the version engine's diff.
            documents = (
                (
                    await session.execute(
                        select(Document).where(Document.opportunity_id == opportunity.id)
                    )
                )
                .scalars()
                .all()
            )
            opportunity.documents = list(documents)
        return opportunity

    async def _create_opportunity(
        self, session: AsyncSession, record: NormalizedOpportunity, run: SourceRun
    ) -> ProcurementOpportunity:
        now = utcnow()
        contact_id = await self._upsert_contact(session, record.contact)
        opportunity = ProcurementOpportunity(
            external_id=record.external_id,
            reference_number=record.reference_number,
            reference_number_normalized=record.reference_number_normalized,
            title=record.title,
            description=record.description,
            procurement_type=str(record.procurement_type),
            status=str(record.status),
            organization=record.organization,
            municipality_id=record.municipality_id,
            province_id=record.province_id,
            source_id=record.source_id,
            published_at=record.published_at,
            closing_at=record.closing_at,
            source_timezone=record.source_timezone,
            raw_dates=record.raw_dates or None,
            estimated_value=record.estimated_value,
            currency=record.currency,
            submission_method=record.submission_method,
            submission_url=record.submission_url,
            submission_address=record.submission_address,
            briefing_required=record.briefing_required,
            briefing_compulsory=record.briefing_compulsory,
            briefing_date=record.briefing_date,
            briefing_location=record.briefing_location,
            contact_id=contact_id,
            source_url=record.source_url,
            canonical_url=record.canonical_url,
            content_hash=record.content_hash,
            fingerprint=record.fingerprint,
            raw_payload=record.raw_payload,
            raw_payload_key=record.raw_payload_key,
            parser_metadata=record.parser_metadata or None,
            data_quality=str(record.data_quality),
            quality_issues=record.quality_issues or None,
            confidence=record.confidence,
            version=1,
            first_seen_at=now,
            last_seen_at=now,
            is_test_fixture=record.is_test_fixture,
        )
        session.add(opportunity)
        await session.flush()

        version = self.versions.build_version(
            opportunity=opportunity,
            record=record,
            version_number=1,
            changed_fields=None,
            source_run_id=run.id,
            observed_at=now,
        )
        session.add(version)
        session.add(self.versions.creation_event(opportunity, occurred_at=now))
        await self._sync_documents(session, opportunity, record, emit_events=False)
        await session.flush()
        return opportunity

    async def _update_existing(
        self,
        session: AsyncSession,
        existing: ProcurementOpportunity,
        record: NormalizedOpportunity,
        run: SourceRun,
    ) -> bool:
        """Apply changes to an existing record. Returns True when it changed."""
        now = utcnow()
        existing.last_seen_at = now
        decision = self.versions.diff(existing, record)
        if not decision.changed:
            if existing.content_hash != record.content_hash:
                # Cosmetic-only change: refresh the hash without a new version.
                existing.content_hash = record.content_hash
            await session.flush()
            return False

        for name in decision.changed_fields:
            if name in {"documents_added", "documents_removed"}:
                continue
            value = getattr(record, name, None)
            if value is None:
                continue
            setattr(existing, name, str(value) if name in {"procurement_type", "status"} else value)

        existing.content_hash = record.content_hash
        existing.fingerprint = record.fingerprint
        existing.data_quality = str(record.data_quality)
        existing.quality_issues = record.quality_issues or None
        existing.confidence = record.confidence
        existing.parser_metadata = record.parser_metadata or None
        existing.raw_dates = record.raw_dates or existing.raw_dates
        existing.version += 1

        if contact_id := await self._upsert_contact(session, record.contact):
            existing.contact_id = contact_id

        version = self.versions.build_version(
            opportunity=existing,
            record=record,
            version_number=existing.version,
            changed_fields=decision.changed_fields,
            source_run_id=run.id,
            observed_at=now,
        )
        session.add(version)
        await session.flush()
        for event in self.versions.build_events(
            opportunity=existing, decision=decision, version=version, occurred_at=now
        ):
            session.add(event)
        await self._sync_documents(session, existing, record, emit_events=True)
        await session.flush()
        return True

    async def _sync_documents(
        self,
        session: AsyncSession,
        opportunity: ProcurementOpportunity,
        record: NormalizedOpportunity,
        *,
        emit_events: bool,
    ) -> None:
        """Register newly discovered document links (download happens later)."""
        from app.db.models.opportunity import OpportunityEvent

        existing_urls = {
            url
            for (url,) in (
                await session.execute(
                    select(Document.source_url).where(Document.opportunity_id == opportunity.id)
                )
            ).all()
        }
        for candidate in record.documents:
            if candidate.source_url in existing_urls:
                continue
            existing_urls.add(candidate.source_url)
            document = Document(
                opportunity_id=opportunity.id,
                source_url=candidate.source_url,
                document_type=str(candidate.document_type),
                document_format=str(candidate.document_format),
                filename=candidate.filename,
                title=candidate.title,
                mime_type=candidate.mime_type,
                published_at=candidate.published_at,
            )
            session.add(document)
            if emit_events:
                session.add(
                    OpportunityEvent(
                        opportunity_id=opportunity.id,
                        event_type=str(EventType.DOCUMENT_ADDED),
                        field="documents",
                        new_value={"url": candidate.source_url},
                        description=(
                            f"Document discovered: {candidate.filename or candidate.source_url}"
                        )[:1000],
                        occurred_at=utcnow(),
                    )
                )

    async def _upsert_contact(
        self, session: AsyncSession, contact: dict[str, Any] | None
    ) -> UUID | None:
        if not contact or not any(contact.get(k) for k in ("name", "email", "phone")):
            return None
        digest = contact_fingerprint(
            name=contact.get("name"),
            email=contact.get("email"),
            phone=contact.get("phone"),
            organization=contact.get("organization"),
        )
        existing = (
            (await session.execute(select(Contact).where(Contact.fingerprint == digest)))
            .scalars()
            .first()
        )
        if existing is not None:
            return existing.id
        row = Contact(
            name=contact.get("name"),
            role=contact.get("role"),
            organization=contact.get("organization"),
            email=contact.get("email"),
            phone=contact.get("phone"),
            fingerprint=digest,
        )
        session.add(row)
        await session.flush()
        return row.id

    # -- finalisation -----------------------------------------------------

    async def _finalise(
        self,
        session: AsyncSession,
        source: MunicipalitySource,
        run: SourceRun,
        stats: RunStats,
        *,
        started: datetime,
        job: IngestionJob | None,
    ) -> None:
        """Persist counters, errors, run status and source health."""
        completed = utcnow()
        duration_ms = int((completed - started).total_seconds() * 1000)

        fatal = any(
            error.stage in {ErrorStage.DISCOVERY, ErrorStage.UNKNOWN} for error in stats.errors
        )
        succeeded = not fatal and (stats.items_found > 0 or not stats.errors)

        run.status = str(JobStatus.COMPLETED if succeeded else JobStatus.FAILED)
        run.completed_at = completed
        run.duration_ms = duration_ms
        run.items_found = stats.items_found
        run.items_created = stats.items_created
        run.items_updated = stats.items_updated
        run.items_skipped = stats.items_skipped
        run.items_failed = stats.items_failed
        run.documents_found = stats.documents_found
        run.stats = stats.as_dict()
        if stats.errors:
            run.error_message = stats.errors[0].message[:2000]

        for error in stats.errors[:200]:
            session.add(
                IngestionError(
                    job_id=job.id if job else None,
                    source_id=source.id,
                    source_run_id=run.id,
                    stage=str(error.stage),
                    error_code=error.code,
                    message=error.message,
                    url=error.url,
                    retryable=error.retryable,
                    context=error.context or None,
                )
            )

        self._update_health(source, succeeded=succeeded, duration_ms=duration_ms, run=run)

        if job is not None:
            job.status = str(JobStatus.COMPLETED if succeeded else JobStatus.FAILED)
            job.completed_at = completed
            job.duration_ms = duration_ms
            job.items_found = stats.items_found
            job.items_created = stats.items_created
            job.items_updated = stats.items_updated
            job.items_skipped = stats.items_skipped
            job.items_failed = stats.items_failed
            job.result = stats.as_dict()
            if not succeeded and stats.errors:
                job.error_message = stats.errors[0].message[:2000]

        logger.info(
            "pipeline.finished",
            source_id=str(source.id),
            status=run.status,
            duration=duration_ms,
            **stats.as_dict(),
        )

    def _update_health(
        self,
        source: MunicipalitySource,
        *,
        succeeded: bool,
        duration_ms: int,
        run: SourceRun,
    ) -> None:
        now = utcnow()
        source.last_run_at = now
        if succeeded:
            source.last_success_at = now
            source.consecutive_failures = 0
            source.health_status = str(HealthStatus.HEALTHY)
        else:
            source.last_failure_at = now
            source.consecutive_failures += 1
            source.health_status = str(_health_for_failures(source.consecutive_failures))

        previous = source.average_response_time_ms
        source.average_response_time_ms = (
            duration_ms if previous is None else round(previous * 0.7 + duration_ms * 0.3, 2)
        )
        if run.http_status:
            source.last_http_status = run.http_status


def _health_for_failures(consecutive_failures: int) -> HealthStatus:
    if consecutive_failures >= 10:
        return HealthStatus.OFFLINE
    if consecutive_failures >= 4:
        return HealthStatus.FAILING
    if consecutive_failures >= 1:
        return HealthStatus.DEGRADED
    return HealthStatus.HEALTHY
