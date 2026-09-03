"""Statistics service.

Aggregations are computed from real data only. Results are cached in-process
for a short TTL so a burst of requests cannot turn the statistics endpoint into
a denial-of-service vector; materialised views can replace this later without
changing the API contract.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models.document import Document, DocumentText
from app.db.models.geography import Municipality, Province
from app.db.models.opportunity import ProcurementOpportunity
from app.db.models.source import MunicipalitySource
from app.enums import OpportunityStatus
from app.schemas.category import CountByKey, StatisticsResponse
from app.utils.dates import utcnow


@dataclass(slots=True)
class _CacheEntry:
    expires_at: float
    payload: StatisticsResponse


class StatisticsService:
    """Computes platform-level aggregates."""

    _cache: _CacheEntry | None = None

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_statistics(self, *, use_cache: bool = True) -> StatisticsResponse:
        ttl = get_settings().statistics_cache_seconds
        now = time.monotonic()
        if (
            use_cache
            and ttl > 0
            and StatisticsService._cache
            and StatisticsService._cache.expires_at > now
        ):
            return StatisticsService._cache.payload

        payload = await self._compute()
        StatisticsService._cache = _CacheEntry(now + ttl, payload) if ttl > 0 else None
        return payload

    @classmethod
    def clear_cache(cls) -> None:
        """Drop the in-process cache (used by tests and by the worker)."""
        cls._cache = None

    async def _compute(self) -> StatisticsResponse:
        moment = utcnow()
        real = ProcurementOpportunity.is_test_fixture.is_(False)

        total = await self._scalar(
            select(func.count()).select_from(ProcurementOpportunity).where(real)
        )
        fixtures = await self._scalar(
            select(func.count())
            .select_from(ProcurementOpportunity)
            .where(ProcurementOpportunity.is_test_fixture.is_(True))
        )
        open_count = await self._scalar(
            select(func.count())
            .select_from(ProcurementOpportunity)
            .where(real, ProcurementOpportunity.status == str(OpportunityStatus.OPEN))
        )
        closing_soon = await self._scalar(
            select(func.count())
            .select_from(ProcurementOpportunity)
            .where(
                real,
                ProcurementOpportunity.closing_at >= moment,
                ProcurementOpportunity.closing_at <= moment + timedelta(days=7),
            )
        )

        documents = await self._scalar(select(func.count()).select_from(Document))
        downloaded = await self._scalar(
            select(func.count()).select_from(Document).where(Document.is_downloaded.is_(True))
        )
        with_text = await self._scalar(select(func.count()).select_from(DocumentText))

        sources = await self._scalar(select(func.count()).select_from(MunicipalitySource))
        active_sources = await self._scalar(
            select(func.count())
            .select_from(MunicipalitySource)
            .where(MunicipalitySource.active.is_(True))
        )
        municipalities = await self._scalar(select(func.count()).select_from(Municipality))
        municipalities_with_sources = await self._scalar(
            select(func.count(func.distinct(MunicipalitySource.municipality_id))).where(
                MunicipalitySource.municipality_id.is_not(None)
            )
        )

        return StatisticsResponse(
            generated_at=moment,
            total_opportunities=total,
            open_opportunities=open_count,
            closing_next_7_days=closing_soon,
            total_documents=documents,
            documents_downloaded=downloaded,
            documents_with_text=with_text,
            total_sources=sources,
            active_sources=active_sources,
            total_municipalities=municipalities,
            municipalities_with_sources=municipalities_with_sources,
            by_province=await self._by_province(),
            by_procurement_type=await self._group_by(ProcurementOpportunity.procurement_type),
            by_status=await self._group_by(ProcurementOpportunity.status),
            by_source_health=await self._source_health(),
            test_fixture_opportunities=fixtures,
        )

    async def _scalar(self, stmt) -> int:  # noqa: ANN001
        return int((await self.session.execute(stmt)).scalar_one() or 0)

    async def _group_by(self, column) -> list[CountByKey]:  # noqa: ANN001
        stmt = (
            select(column, func.count())
            .where(ProcurementOpportunity.is_test_fixture.is_(False))
            .group_by(column)
            .order_by(func.count().desc())
            .limit(50)
        )
        rows = (await self.session.execute(stmt)).all()
        return [CountByKey(key=str(key), count=int(count)) for key, count in rows]

    async def _by_province(self) -> list[CountByKey]:
        stmt = (
            select(Province.code, Province.name, func.count(ProcurementOpportunity.id))
            .select_from(ProcurementOpportunity)
            .join(
                Municipality,
                Municipality.id == ProcurementOpportunity.municipality_id,
                isouter=True,
            )
            .join(
                Province,
                Province.id
                == func.coalesce(ProcurementOpportunity.province_id, Municipality.province_id),
            )
            .where(ProcurementOpportunity.is_test_fixture.is_(False))
            .group_by(Province.code, Province.name)
            .order_by(func.count(ProcurementOpportunity.id).desc())
        )
        rows = (await self.session.execute(stmt)).all()
        return [
            CountByKey(key=str(code), label=str(name), count=int(count))
            for code, name, count in rows
        ]

    async def _source_health(self) -> list[CountByKey]:
        stmt = (
            select(MunicipalitySource.health_status, func.count())
            .group_by(MunicipalitySource.health_status)
            .order_by(func.count().desc())
        )
        rows = (await self.session.execute(stmt)).all()
        return [CountByKey(key=str(status), count=int(count)) for status, count in rows]
