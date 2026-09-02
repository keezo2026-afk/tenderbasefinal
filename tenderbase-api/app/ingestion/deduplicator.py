"""Layered deduplication.

Layer 1 — issuer + reference number (strongest signal)
Layer 2 — content hash (byte-identical canonical content)
Layer 3 — secondary fingerprint (title/organization/closing date/type)
Layer 4 — optional fuzzy title similarity (PostgreSQL trigram, ``pg_trgm``)

Uncertain matches are **never** merged automatically: they are returned as
``UNCERTAIN`` with the candidate attached, so the record can be persisted
separately and reviewed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.opportunity import ProcurementOpportunity
from app.enums import DuplicateDecision
from app.logging import get_logger
from app.schemas.tender import NormalizedOpportunity

logger = get_logger("tenderbase.deduplicator")

#: Similarity above which a trigram title match is considered *probable*.
FUZZY_PROBABLE_THRESHOLD = 0.82
#: Similarity above which a match is flagged for review rather than ignored.
FUZZY_REVIEW_THRESHOLD = 0.65
#: Closing dates must be within this window to support a fuzzy match.
FUZZY_CLOSING_WINDOW = timedelta(days=3)


@dataclass(slots=True)
class DedupResult:
    """Outcome of the deduplication decision for one incoming record."""

    decision: DuplicateDecision
    existing_id: UUID | None = None
    layer: str | None = None
    confidence: float = 0.0
    reason: str | None = None

    @property
    def is_duplicate(self) -> bool:
        """Only exact/probable matches are treated as the *same* opportunity."""
        return self.decision in (DuplicateDecision.EXACT_MATCH, DuplicateDecision.PROBABLE_MATCH)


class Deduplicator:
    """Resolves whether an incoming record already exists."""

    def __init__(self, *, enable_fuzzy: bool = True) -> None:
        self.enable_fuzzy = enable_fuzzy

    async def find_duplicate(
        self, session: AsyncSession, record: NormalizedOpportunity
    ) -> DedupResult:
        """Run the dedup layers in order of decreasing certainty."""
        for layer in (self._layer1, self._layer2, self._layer3):
            result = await layer(session, record)
            if result is not None:
                return result
        if self.enable_fuzzy:
            if (result := await self._layer4(session, record)) is not None:
                return result
        return DedupResult(DuplicateDecision.NEW, confidence=1.0, layer="none")

    # -- layers -----------------------------------------------------------

    async def _layer1(
        self, session: AsyncSession, record: NormalizedOpportunity
    ) -> DedupResult | None:
        """Issuer scope + reference number."""
        reference = record.reference_number_normalized or record.reference_number
        if not reference:
            return None
        scope = []
        if record.municipality_id is not None:
            scope.append(ProcurementOpportunity.municipality_id == record.municipality_id)
        else:
            scope.append(ProcurementOpportunity.source_id == record.source_id)
        stmt = self._base_select().where(
            and_(
                or_(
                    ProcurementOpportunity.reference_number_normalized == reference,
                    ProcurementOpportunity.reference_number == record.reference_number,
                ),
                *scope,
            )
        )
        existing = (await session.execute(stmt)).scalars().first()
        if existing is None:
            return None
        return DedupResult(
            DuplicateDecision.EXACT_MATCH,
            existing_id=existing.id,
            layer="reference_number",
            confidence=0.99,
            reason="matched issuer scope + reference number",
        )

    async def _layer2(
        self, session: AsyncSession, record: NormalizedOpportunity
    ) -> DedupResult | None:
        """Identical canonical content within the same source."""
        if not record.content_hash:
            return None
        stmt = self._base_select().where(
            ProcurementOpportunity.content_hash == record.content_hash,
            ProcurementOpportunity.source_id == record.source_id,
        )
        existing = (await session.execute(stmt)).scalars().first()
        if existing is None:
            return None
        return DedupResult(
            DuplicateDecision.EXACT_MATCH,
            existing_id=existing.id,
            layer="content_hash",
            confidence=1.0,
            reason="identical content hash",
        )

    async def _layer3(
        self, session: AsyncSession, record: NormalizedOpportunity
    ) -> DedupResult | None:
        """Secondary fingerprint over normalized identity fields."""
        if not record.fingerprint:
            return None
        stmt = self._base_select().where(ProcurementOpportunity.fingerprint == record.fingerprint)
        existing = (await session.execute(stmt)).scalars().first()
        if existing is None:
            return None
        same_scope = existing.source_id == record.source_id or (
            record.municipality_id is not None
            and existing.municipality_id == record.municipality_id
        )
        if same_scope:
            return DedupResult(
                DuplicateDecision.PROBABLE_MATCH,
                existing_id=existing.id,
                layer="fingerprint",
                confidence=0.9,
                reason="matching normalized fingerprint in the same scope",
            )
        # Same opportunity text from a different issuer: do NOT merge.
        return DedupResult(
            DuplicateDecision.UNCERTAIN,
            existing_id=existing.id,
            layer="fingerprint",
            confidence=0.5,
            reason="fingerprint matches a record from a different source/municipality",
        )

    async def _layer4(
        self, session: AsyncSession, record: NormalizedOpportunity
    ) -> DedupResult | None:
        """Optional fuzzy title match (PostgreSQL trigram similarity)."""
        dialect = session.bind.dialect.name if session.bind is not None else ""
        if dialect != "postgresql" or not record.title:
            return None

        similarity = func.similarity(ProcurementOpportunity.title, record.title)
        stmt = (
            select(ProcurementOpportunity, similarity.label("score"))
            .where(ProcurementOpportunity.source_id == record.source_id)
            .where(similarity > FUZZY_REVIEW_THRESHOLD)
        )
        if record.closing_at is not None:
            stmt = stmt.where(
                or_(
                    ProcurementOpportunity.closing_at.is_(None),
                    and_(
                        ProcurementOpportunity.closing_at
                        >= record.closing_at - FUZZY_CLOSING_WINDOW,
                        ProcurementOpportunity.closing_at
                        <= record.closing_at + FUZZY_CLOSING_WINDOW,
                    ),
                )
            )
        stmt = stmt.order_by(similarity.desc()).limit(1)

        try:
            row = (await session.execute(stmt)).first()
        except Exception as exc:  # noqa: BLE001 - pg_trgm may not be installed
            logger.warning("dedup.fuzzy_unavailable", error=str(exc))
            return None
        if row is None:
            return None

        existing, score = row[0], float(row[1] or 0.0)
        if score >= FUZZY_PROBABLE_THRESHOLD:
            return DedupResult(
                DuplicateDecision.PROBABLE_MATCH,
                existing_id=existing.id,
                layer="trigram",
                confidence=round(score, 3),
                reason=f"title similarity {score:.2f}",
            )
        return DedupResult(
            DuplicateDecision.UNCERTAIN,
            existing_id=existing.id,
            layer="trigram",
            confidence=round(score, 3),
            reason=f"title similarity {score:.2f} below the auto-merge threshold",
        )

    def _base_select(self) -> Select[tuple[ProcurementOpportunity]]:
        return select(ProcurementOpportunity).limit(1)
