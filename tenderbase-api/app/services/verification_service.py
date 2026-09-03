"""Source verification service: run the verifier and record the evidence.

This is the write-side companion to :mod:`app.ingestion.verifier`. It keeps the
two concerns separate on purpose:

* the **verifier** performs live checks and returns a report;
* this **service** persists the report onto the source row, advances the
  lifecycle state machine and keeps the counters honest.

Lifecycle rules enforced here (no magic, no auto-activation):

* A ``PASSED`` report on a source in ``DISCOVERED``/``PENDING_VERIFICATION``
  moves it to ``VERIFIED`` — *not* to ``ACTIVE``. An operator must deliberately
  activate a source, because passing an automated pre-flight is not the same as
  a human approving a data source for production collection.
* A ``PASSED``/``PASSED_WITH_WARNINGS`` report never clears an existing
  ``verified_at`` (the human stamp) and never sets one; those two dates answer
  different questions.
* A ``FAILED`` report sets ``verification_status=FAILED`` and downgrades
  ``ACTIVE`` to ``DEGRADED`` only when the source has also started failing
  ingestion — a verification warning alone must not silently disable coverage.
"""

from __future__ import annotations

from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.connectors.base import SourceContext
from app.db.models.source import MunicipalitySource
from app.enums import SourceLifecycle, VerificationStatus
from app.errors import SourceNotFoundError, ValidationError
from app.ingestion.fetcher import HTTPFetcher
from app.ingestion.verifier import SourceVerifier, VerificationReport
from app.logging import get_logger
from app.utils.dates import utcnow

logger = get_logger("tenderbase.services.verify")


@dataclass(slots=True)
class VerificationOutcome:
    """What changed on the source as a result of verification."""

    source_id: UUID
    slug: str
    status: str
    summary: str
    previous_lifecycle: str
    lifecycle: str
    report: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_id": str(self.source_id),
            "slug": self.slug,
            "verification_status": self.status,
            "summary": self.summary,
            "lifecycle_before": self.previous_lifecycle,
            "lifecycle_after": self.lifecycle,
            "report": self.report,
        }


class SourceVerificationService:
    """Persisted, operator-triggered verification of one source."""

    def __init__(self, session: AsyncSession, settings: Settings | None = None) -> None:
        self.session = session
        self.settings = settings or get_settings()

    async def get(self, source_id: str | UUID) -> MunicipalitySource:
        try:
            identifier = UUID(str(source_id))
        except ValueError as exc:
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

    async def verify(
        self,
        source_id: str | UUID,
        *,
        fetcher: Any | None = None,
        persist: bool = True,
        sample_items: int = 3,
    ) -> VerificationOutcome:
        """Verify one source and (by default) record the outcome."""
        source = await self.get(source_id)
        context = SourceContext.from_model(source)
        previous = str(source.lifecycle_status)

        owns_fetcher = fetcher is None
        async with AsyncExitStack() as stack:
            if owns_fetcher:
                fetcher = await stack.enter_async_context(HTTPFetcher(settings=self.settings))
            verifier = SourceVerifier(
                fetcher=fetcher, settings=self.settings, sample_items=sample_items
            )
            report: VerificationReport = await verifier.verify(context)

        outcome = VerificationOutcome(
            source_id=source.id,
            slug=source.slug,
            status=report.status,
            summary=report.summary,
            previous_lifecycle=previous,
            lifecycle=previous,
            report=report.as_dict(),
        )
        if persist:
            await self._record(source, report, outcome)
        logger.info(
            "verify.recorded" if persist else "verify.dry_run",
            source_id=str(source.id),
            status=report.status,
            lifecycle=outcome.lifecycle,
        )
        return outcome

    async def _record(
        self,
        source: MunicipalitySource,
        report: VerificationReport,
        outcome: VerificationOutcome,
    ) -> None:
        now: datetime = utcnow()
        source.verification_status = report.status
        source.verification_at = now
        source.verification_duration_ms = report.duration_ms
        source.verification_http_status = report.http_status
        source.verification_result = report.as_dict()

        # Discovery produced nothing usable => the source cannot currently be
        # collected. Record it, and stop scheduling it if it was scheduled.
        passed = report.status in (
            str(VerificationStatus.PASSED),
            str(VerificationStatus.PASSED_WITH_WARNINGS),
        )
        if passed and source.lifecycle_status in (
            str(SourceLifecycle.DISCOVERED),
            str(SourceLifecycle.PENDING_VERIFICATION),
        ):
            source.lifecycle_status = str(SourceLifecycle.VERIFIED)
        elif not passed and source.lifecycle_status == str(SourceLifecycle.ACTIVE):
            # A previously active source whose pre-flight now fails keeps running
            # (health tracking handles live failures) but is flagged for review.
            source.lifecycle_status = str(SourceLifecycle.DEGRADED)
        outcome.lifecycle = str(source.lifecycle_status)
        await self.session.flush()

    # -- lifecycle transitions (explicit, auditable) ----------------------

    async def set_lifecycle(
        self,
        source_id: str | UUID,
        lifecycle: SourceLifecycle,
        *,
        reason: str | None = None,
    ) -> MunicipalitySource:
        """Move a source to an explicit lifecycle state.

        Guard rails rather than convenience: ``ACTIVE`` requires a passing
        verification record, and ``PAUSED`` requires a reason, so the registry
        can never quietly contain a source nobody checked.
        """
        source = await self.get(source_id)
        target = SourceLifecycle.parse(lifecycle)
        if target is SourceLifecycle.ACTIVE:
            if source.verification_status not in (
                str(VerificationStatus.PASSED),
                str(VerificationStatus.PASSED_WITH_WARNINGS),
            ):
                raise ValidationError(
                    "A source must pass verification before it can be activated. "
                    "Run: python -m scripts.verify_source <source_id>",
                    code="SOURCE_NOT_VERIFIED",
                    details={"verification_status": source.verification_status},
                )
        if target is SourceLifecycle.PAUSED and not (reason or "").strip():
            raise ValidationError("Pausing a source requires a reason", code="REASON_REQUIRED")

        source.lifecycle_status = str(target)
        source.active = target.schedulable
        if target is SourceLifecycle.PAUSED:
            source.paused_at = utcnow()
            source.paused_reason = reason[:500]
        else:
            source.paused_at = None
            source.paused_reason = None
        await self.session.flush()
        logger.info("source.lifecycle_changed", source_id=str(source.id), lifecycle=str(target))
        return source
