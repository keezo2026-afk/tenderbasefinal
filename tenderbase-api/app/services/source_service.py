"""Source registry service."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models.geography import Province
from app.db.models.source import MunicipalitySource, SourceRun
from app.errors import SourceNotFoundError
from app.schemas.common import PaginationParams
from app.schemas.source import SourceFilter


class SourceService:
    """Read-side service for procurement sources and their runs."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_sources(
        self, filters: SourceFilter, pagination: PaginationParams
    ) -> tuple[list[MunicipalitySource], int]:
        stmt = select(MunicipalitySource)
        if filters.source_type:
            stmt = stmt.where(MunicipalitySource.source_type == str(filters.source_type))
        if filters.connector_type:
            stmt = stmt.where(MunicipalitySource.connector_type == str(filters.connector_type))
        if filters.health_status:
            stmt = stmt.where(MunicipalitySource.health_status == str(filters.health_status))
        if filters.municipality_id:
            stmt = stmt.where(MunicipalitySource.municipality_id == filters.municipality_id)
        if filters.active is not None:
            stmt = stmt.where(MunicipalitySource.active.is_(filters.active))
        if filters.province:
            lowered = filters.province.lower()
            province_ids = select(Province.id).where(
                or_(
                    func.lower(Province.slug) == lowered,
                    func.lower(Province.code) == lowered,
                    func.lower(Province.name) == lowered,
                )
            )
            stmt = stmt.where(MunicipalitySource.province_id.in_(province_ids))
        if filters.q:
            pattern = f"%{filters.q}%"
            stmt = stmt.where(
                or_(
                    MunicipalitySource.name.ilike(pattern),
                    MunicipalitySource.organization.ilike(pattern),
                )
            )

        total = await self._count(stmt)
        stmt = (
            stmt.options(
                selectinload(MunicipalitySource.municipality),
                selectinload(MunicipalitySource.province),
            )
            .order_by(MunicipalitySource.priority.asc(), MunicipalitySource.name.asc())
            .offset(pagination.offset)
            .limit(pagination.limit)
        )
        return list((await self.session.execute(stmt)).unique().scalars().all()), total

    async def get_source(self, source_id: UUID) -> MunicipalitySource:
        stmt = (
            select(MunicipalitySource)
            .where(MunicipalitySource.id == source_id)
            .options(
                selectinload(MunicipalitySource.municipality),
                selectinload(MunicipalitySource.province),
            )
        )
        source = (await self.session.execute(stmt)).unique().scalars().first()
        if source is None:
            raise SourceNotFoundError(details={"id": str(source_id)})
        return source

    async def list_runs(
        self, source_id: UUID, pagination: PaginationParams
    ) -> tuple[list[SourceRun], int]:
        await self.get_source(source_id)
        base = select(SourceRun).where(SourceRun.source_id == source_id)
        total = await self._count(base)
        stmt = (
            base.order_by(SourceRun.started_at.desc().nulls_last(), SourceRun.id.asc())
            .offset(pagination.offset)
            .limit(pagination.limit)
        )
        return list((await self.session.execute(stmt)).scalars().all()), total

    async def _count(self, stmt: Select[Any]) -> int:
        subquery = stmt.order_by(None).subquery()
        return int(
            (await self.session.execute(select(func.count()).select_from(subquery))).scalar_one()
        )
