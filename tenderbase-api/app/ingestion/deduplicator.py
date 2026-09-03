"""Layered deduplication.

Layer 1 — issuer scope + reference number (strongest signal)
Layer 2 — content hash (byte-identical canonical content, same source)
Layer 3 — secondary fingerprint (title/organization/closing date/type)
Layer 4 — optional fuzzy title similarity (PostgreSQL trigram, ``pg_trgm``)

Uncertain matches are **never** merged automatically: they are returned as
``UNCERTAIN`` with the candidate attached, so the record can be persisted
separately and reviewed.

Re-advertisement vs duplicate
------------------------------
A tender that appears again is not automatically a duplicate, and this module is
where that distinction is decided:

* a *different* reference number from the same issuer is evidence of a **new**
  tender (an extension keeps the reference; a re-advertisement normally does
  not) — so a fingerprint or trigram match is rejected when both records carry
  conflicting reference numbers;
* a *different* municipality/source is evidence of a different **issuer**, so a
  text match is downgraded to ``UNCERTAIN`` rather than merged;
* a matching reference number with changed dates/text is the *same* tender and
  belongs in the version history instead — that is decided by the caller (the
  pipeline routes an already-known ``existing_id`` to the version engine).

The result is that nothing here destroys history and nothing is merged on
similarity alone.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.opportunity import ProcurementOpportunity
from app.enums import DuplicateDecision
from app.logging import get_logger
from app.observability import metrics
from app.schemas.tender import NormalizedOpportunity
from app.utils.text import normalize_reference_number

logger = get_logger("tenderbase.deduplicator")

#: Similarity above which a trigram title match is considered *probable*.
FUZZY_PROBABLE_THRESHOLD = 0.82
#: Similarity above which a match is flagged for review rather than ignored.
FUZZY_REVIEW_THRESHOLD = 0.65
#: Closing dates must be within this window to support a fuzzy match.
FUZZY_CLOSING_WINDOW = timedelta(days=3)
#: Titles shorter than this carry too little signal for trigram matching.
FUZZY_MIN_TITLE_LENGTH = 25


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
        result = await self._layer1(session, record)
        if result is not None:
            return self._observe(result)
        for layer in (self._layer2, self._layer3):
            result = await layer(session, record)
            if result is None:
                continue
            # Layers 2/3 can surface a candidate that the issuer's own
            # reference numbers say is a *different* tender.
            if result.existing_id is not None and await self._conflicts_with(
                session, record, result.existing_id
            ):
                continue
            return self._observe(result)
        if self.enable_fuzzy:
            result = await self._layer4(session, record)
            if result is not None:
                return self._observe(result)
        return self._observe(DedupResult(DuplicateDecision.NEW, confidence=1.0, layer="none"))

    @staticmethod
    def _observe(result: DedupResult) -> DedupResult:
        metrics.observe_dedup(
            layer=result.layer or "none",
            decision=str(result.decision),
            uncertain=int(result.decision is DuplicateDecision.UNCERTAIN),
        )
        return result

    # -- issuer-intent guard ----------------------------------------------

    async def _conflicts_with(
        self, session: AsyncSession, record: NormalizedOpportunity, existing_id: UUID
    ) -> bool:
        """True when the candidate is provably a *different* tender.

        Two published reference numbers that differ are the strongest available
        signal of separate procurements, stronger than a text match. Checking it
        here stops a re-advertised tender from being swallowed by the similar
        original.
        """
        incoming = _reference_key(record.reference_number_normalized, record.reference_number)
        if incoming is None:
            return False
        existing_ref = (
            await session.execute(
                select(
                    ProcurementOpportunity.reference_number_normalized,
                    ProcurementOpportunity.reference_number,
                ).where(ProcurementOpportunity.id == existing_id)
            )
        ).first()
        if existing_ref is None:
            return False
        existing = _reference_key(existing_ref[0], existing_ref[1])
        return existing is not None and existing != incoming

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
        if not same_scope:
            # Same opportunity text from a different issuer: do NOT merge.
            return DedupResult(
                DuplicateDecision.UNCERTAIN,
                existing_id=existing.id,
                layer="fingerprint",
                confidence=0.5,
                reason="fingerprint matches a record from a different source/municipality",
            )
        # A different municipality under the same source is still a different
        # issuer: hold for review instead of merging.
        if (
            record.municipality_id is not None
            and existing.municipality_id is not None
            and existing.municipality_id != record.municipality_id
        ):
            return DedupResult(
                DuplicateDecision.UNCERTAIN,
                existing_id=existing.id,
                layer="fingerprint",
                confidence=0.45,
                reason="fingerprint matches a record of a different municipality",
            )
        return DedupResult(
            DuplicateDecision.PROBABLE_MATCH,
            existing_id=existing.id,
            layer="fingerprint",
            confidence=0.9,
            reason="matching normalized fingerprint in the same scope",
        )

    async def _layer4(
        self, session: AsyncSession, record: NormalizedOpportunity
    ) -> DedupResult | None:
        """Optional fuzzy title match (PostgreSQL trigram similarity)."""
        dialect = session.bind.dialect.name if session.bind is not None else ""
        if dialect != "postgresql" or not record.title:
            return None
        if len(record.title.strip()) < FUZZY_MIN_TITLE_LENGTH:
            return None

        similarity = func.similarity(ProcurementOpportunity.title, record.title)
        stmt = (
            select(ProcurementOpportunity, similarity.label("score"))
            .where(ProcurementOpportunity.source_id == record.source_id)
            .where(similarity > FUZZY_REVIEW_THRESHOLD)
        )
        # Fuzzy text similarity is the weakest signal in the system: it must
        # never override an explicit issuer difference. Different municipality,
        # or a conflicting published reference number, means "different tender".
        if record.municipality_id is not None:
            stmt = stmt.where(ProcurementOpportunity.municipality_id == record.municipality_id)
        if record.reference_number_normalized:
            stmt = stmt.where(
                or_(
                    ProcurementOpportunity.reference_number_normalized.is_(None),
                    ProcurementOpportunity.reference_number_normalized
                    == record.reference_number_normalized,
                )
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

    def _base_select(self) -> Select[Any]:
        return select(ProcurementOpportunity).limit(1)


def _reference_key(normalized: str | None, raw: str | None) -> str | None:
    """Comparable reference, or ``None`` when the source published none.

    ``normalized`` is already the canonical form produced by the normalizer; the
    raw value is only consulted when a source published a reference but the
    normalized column is unset (older rows).
    """
    return normalize_reference_number(normalized or raw)
