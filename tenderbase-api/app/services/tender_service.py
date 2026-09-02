"""Tender (procurement opportunity) service.

All query construction lives here — routes stay thin and the API contract is
decoupled from the ORM. Every query is bounded: no endpoint can trigger an
unrestricted table scan through the public API.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Any
from uuid import UUID

from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models.category import Category, OpportunityCategory
from app.db.models.document import Document
from app.db.models.geography import District, Municipality, Province
from app.db.models.opportunity import (
    OpportunityEvent,
    OpportunityVersion,
    ProcurementOpportunity,
)
from app.errors import TenderNotFoundError
from app.schemas.common import PaginationParams
from app.schemas.tender import SearchQuery, TenderFilter
from app.search.service import SearchBackend, SearchResponse, SearchResult, get_search_backend
from app.utils.dates import ensure_utc, utcnow

MAX_EVENTS = 500

SORT_COLUMNS = {
    "published_at": ProcurementOpportunity.published_at,
    "closing_at": ProcurementOpportunity.closing_at,
    "created_at": ProcurementOpportunity.created_at,
    "last_seen_at": ProcurementOpportunity.last_seen_at,
    "title": ProcurementOpportunity.title,
}


@dataclass(slots=True)
class TenderPage:
    """A page of opportunities."""

    items: list[ProcurementOpportunity]
    total: int


def _as_datetime(value: date | datetime | None, *, end_of_day: bool = False) -> datetime | None:
    """Coerce a date filter into a timezone-aware UTC datetime."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return ensure_utc(value, assume_timezone="UTC")
    moment = datetime.combine(value, time(23, 59, 59) if end_of_day else time(0, 0))
    return ensure_utc(moment, assume_timezone="UTC")


class TenderService:
    """Read-side service for procurement opportunities."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # -- collection -------------------------------------------------------

    async def list_tenders(self, filters: TenderFilter, pagination: PaginationParams) -> TenderPage:
        """List opportunities matching ``filters`` with deterministic ordering."""
        stmt = select(ProcurementOpportunity)
        stmt = await self._apply_filters(stmt, filters)

        rank = None
        if filters.q:
            backend = get_search_backend(self.session)
            stmt, rank = backend.apply_text_filter(stmt, filters.q)

        total = await self._count(stmt)
        stmt = self._apply_sort(stmt, filters.sort, rank)
        stmt = stmt.offset(pagination.offset).limit(pagination.limit)
        stmt = stmt.options(
            selectinload(ProcurementOpportunity.municipality).selectinload(Municipality.province),
            selectinload(ProcurementOpportunity.province),
            selectinload(ProcurementOpportunity.source),
        )
        items = list((await self.session.execute(stmt)).unique().scalars().all())
        return TenderPage(items=items, total=total)

    async def search(self, query: SearchQuery, pagination: PaginationParams) -> SearchResponse:
        """Full-text search with the same filter surface as the list endpoint."""
        started = utcnow()
        backend: SearchBackend = get_search_backend(self.session)

        stmt = select(ProcurementOpportunity)
        stmt = await self._apply_filters(stmt, query)
        stmt, rank = backend.apply_text_filter(stmt, query.q)

        total = await self._count(stmt)
        stmt = self._apply_sort(stmt, query.sort, rank)

        if rank is not None:
            stmt = stmt.add_columns(rank.label("score"))
        stmt = (
            stmt.offset(pagination.offset)
            .limit(pagination.limit)
            .options(
                selectinload(ProcurementOpportunity.municipality).selectinload(
                    Municipality.province
                ),
                selectinload(ProcurementOpportunity.province),
                selectinload(ProcurementOpportunity.source),
            )
        )

        rows = (await self.session.execute(stmt)).unique().all()
        results: list[SearchResult] = []
        for row in rows:
            opportunity = row[0]
            score = float(row[1]) if len(row) > 1 and row[1] is not None else None
            results.append(
                SearchResult(
                    opportunity=opportunity,
                    score=score,
                    snippet=backend.snippet(opportunity, query.q),
                )
            )
        return SearchResponse(
            results=results,
            total=total,
            backend=backend.name,
            took_ms=round((utcnow() - started).total_seconds() * 1000, 2),
        )

    # -- single record ----------------------------------------------------

    async def get_tender(self, tender_id: UUID) -> ProcurementOpportunity:
        """Fetch one opportunity with its documents and categories."""
        stmt = (
            select(ProcurementOpportunity)
            .where(ProcurementOpportunity.id == tender_id)
            .options(
                selectinload(ProcurementOpportunity.municipality).selectinload(
                    Municipality.province
                ),
                selectinload(ProcurementOpportunity.province),
                selectinload(ProcurementOpportunity.source),
                selectinload(ProcurementOpportunity.contact),
                selectinload(ProcurementOpportunity.documents),
                selectinload(ProcurementOpportunity.categories).selectinload(
                    OpportunityCategory.category
                ),
            )
        )
        opportunity = (await self.session.execute(stmt)).unique().scalars().first()
        if opportunity is None:
            raise TenderNotFoundError(details={"id": str(tender_id)})
        return opportunity

    async def get_documents(self, tender_id: UUID) -> list[Document]:
        await self._ensure_exists(tender_id)
        stmt = (
            select(Document)
            .where(Document.opportunity_id == tender_id)
            .order_by(Document.created_at.asc())
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def get_events(
        self, tender_id: UUID, pagination: PaginationParams
    ) -> tuple[list[OpportunityEvent], int]:
        await self._ensure_exists(tender_id)
        base = select(OpportunityEvent).where(OpportunityEvent.opportunity_id == tender_id)
        total = await self._count(base)
        stmt = (
            base.order_by(OpportunityEvent.occurred_at.desc(), OpportunityEvent.id.desc())
            .offset(pagination.offset)
            .limit(min(pagination.limit, MAX_EVENTS))
        )
        return list((await self.session.execute(stmt)).scalars().all()), total

    async def get_versions(
        self, tender_id: UUID, pagination: PaginationParams
    ) -> tuple[list[OpportunityVersion], int]:
        await self._ensure_exists(tender_id)
        base = select(OpportunityVersion).where(OpportunityVersion.opportunity_id == tender_id)
        total = await self._count(base)
        stmt = (
            base.order_by(OpportunityVersion.version.desc())
            .offset(pagination.offset)
            .limit(pagination.limit)
        )
        return list((await self.session.execute(stmt)).scalars().all()), total

    # -- internals --------------------------------------------------------

    async def _ensure_exists(self, tender_id: UUID) -> None:
        exists = (
            (
                await self.session.execute(
                    select(ProcurementOpportunity.id).where(ProcurementOpportunity.id == tender_id)
                )
            )
            .scalars()
            .first()
        )
        if exists is None:
            raise TenderNotFoundError(details={"id": str(tender_id)})

    async def _count(self, stmt: Select[Any]) -> int:
        subquery = stmt.order_by(None).subquery()
        return int(
            (await self.session.execute(select(func.count()).select_from(subquery))).scalar_one()
        )

    async def _apply_filters(self, stmt: Select[Any], filters: TenderFilter) -> Select[Any]:
        if not filters.include_test_fixtures:
            stmt = stmt.where(ProcurementOpportunity.is_test_fixture.is_(False))
        if filters.status:
            stmt = stmt.where(ProcurementOpportunity.status == str(filters.status))
        if filters.type:
            stmt = stmt.where(ProcurementOpportunity.procurement_type == str(filters.type))
        if filters.data_quality:
            stmt = stmt.where(ProcurementOpportunity.data_quality == str(filters.data_quality))
        if filters.source_id:
            stmt = stmt.where(ProcurementOpportunity.source_id == filters.source_id)
        if filters.municipality_id:
            stmt = stmt.where(ProcurementOpportunity.municipality_id == filters.municipality_id)
        if filters.reference_number:
            from app.utils.text import normalize_reference_number

            normalized = normalize_reference_number(filters.reference_number)
            stmt = stmt.where(
                or_(
                    ProcurementOpportunity.reference_number == filters.reference_number,
                    ProcurementOpportunity.reference_number_normalized == normalized,
                )
            )

        if filters.province:
            province_ids = await self._province_ids(filters.province)
            municipality_ids = select(Municipality.id).where(
                Municipality.province_id.in_(province_ids)
            )
            stmt = stmt.where(
                or_(
                    ProcurementOpportunity.province_id.in_(province_ids),
                    ProcurementOpportunity.municipality_id.in_(municipality_ids),
                )
            )
        if filters.district:
            district_ids = select(District.id).where(
                or_(
                    func.lower(District.name) == filters.district.lower(),
                    func.lower(District.code) == filters.district.lower(),
                    func.lower(District.slug) == filters.district.lower(),
                )
            )
            municipality_ids = select(Municipality.id).where(
                Municipality.district_id.in_(district_ids)
            )
            stmt = stmt.where(ProcurementOpportunity.municipality_id.in_(municipality_ids))
        if filters.municipality:
            municipality_ids = select(Municipality.id).where(
                or_(
                    func.lower(Municipality.name) == filters.municipality.lower(),
                    func.lower(Municipality.code) == filters.municipality.lower(),
                    func.lower(Municipality.slug) == filters.municipality.lower(),
                )
            )
            stmt = stmt.where(ProcurementOpportunity.municipality_id.in_(municipality_ids))
        if filters.category:
            category_ids = select(Category.id).where(
                func.lower(Category.slug) == filters.category.lower()
            )
            opportunity_ids = select(OpportunityCategory.opportunity_id).where(
                OpportunityCategory.category_id.in_(category_ids)
            )
            stmt = stmt.where(ProcurementOpportunity.id.in_(opportunity_ids))

        if value := _as_datetime(filters.published_after):
            stmt = stmt.where(ProcurementOpportunity.published_at >= value)
        if value := _as_datetime(filters.published_before, end_of_day=True):
            stmt = stmt.where(ProcurementOpportunity.published_at <= value)
        if value := _as_datetime(filters.closing_after):
            stmt = stmt.where(ProcurementOpportunity.closing_at >= value)
        if value := _as_datetime(filters.closing_before, end_of_day=True):
            stmt = stmt.where(ProcurementOpportunity.closing_at <= value)

        if filters.min_value is not None:
            stmt = stmt.where(ProcurementOpportunity.estimated_value >= filters.min_value)
        if filters.max_value is not None:
            stmt = stmt.where(ProcurementOpportunity.estimated_value <= filters.max_value)
        return stmt

    async def _province_ids(self, value: str) -> Select[Any]:
        lowered = value.lower()
        return select(Province.id).where(
            or_(
                func.lower(Province.name) == lowered,
                func.lower(Province.code) == lowered,
                func.lower(Province.slug) == lowered,
            )
        )

    def _apply_sort(self, stmt: Select[Any], sort: str, rank: Any) -> Select[Any]:
        descending = sort.startswith("-")
        key = sort.lstrip("-")

        if key == "relevance":
            if rank is not None:
                return stmt.order_by(rank.desc(), ProcurementOpportunity.id.asc())
            key, descending = "published_at", True

        column = SORT_COLUMNS.get(key, ProcurementOpportunity.published_at)
        ordering = column.desc().nulls_last() if descending else column.asc().nulls_last()
        # A tiebreaker keeps pagination deterministic across pages.
        return stmt.order_by(ordering, ProcurementOpportunity.id.asc())
