"""Geography service: provinces, districts and municipalities."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models.geography import District, Municipality, Province
from app.errors import MunicipalityNotFoundError, NotFoundError
from app.schemas.common import PaginationParams
from app.schemas.municipality import MunicipalityFilter


class MunicipalityService:
    """Read-side service for the geographic hierarchy."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_provinces(self) -> list[Province]:
        stmt = select(Province).order_by(Province.name.asc())
        return list((await self.session.execute(stmt)).scalars().all())

    async def get_province(self, identifier: str) -> Province:
        lowered = identifier.lower()
        stmt = select(Province).where(
            or_(
                func.lower(Province.slug) == lowered,
                func.lower(Province.code) == lowered,
                func.lower(Province.name) == lowered,
            )
        )
        province = (await self.session.execute(stmt)).scalars().first()
        if province is None:
            raise NotFoundError("Province not found", code="PROVINCE_NOT_FOUND")
        return province

    async def list_districts(self, province: str | None = None) -> list[District]:
        stmt = select(District).options(selectinload(District.province)).order_by(District.name)
        if province:
            lowered = province.lower()
            stmt = stmt.join(Province).where(
                or_(
                    func.lower(Province.slug) == lowered,
                    func.lower(Province.code) == lowered,
                    func.lower(Province.name) == lowered,
                )
            )
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_municipalities(
        self, filters: MunicipalityFilter, pagination: PaginationParams
    ) -> tuple[list[Municipality], int]:
        stmt = select(Municipality)
        if filters.province:
            lowered = filters.province.lower()
            province_ids = select(Province.id).where(
                or_(
                    func.lower(Province.slug) == lowered,
                    func.lower(Province.code) == lowered,
                    func.lower(Province.name) == lowered,
                )
            )
            stmt = stmt.where(Municipality.province_id.in_(province_ids))
        if filters.type:
            stmt = stmt.where(Municipality.type == str(filters.type))
        if filters.active is not None:
            stmt = stmt.where(Municipality.active.is_(filters.active))
        if filters.q:
            stmt = stmt.where(Municipality.name.ilike(f"%{filters.q}%"))

        total = await self._count(stmt)
        stmt = (
            stmt.options(selectinload(Municipality.province), selectinload(Municipality.district))
            .order_by(Municipality.name.asc(), Municipality.id.asc())
            .offset(pagination.offset)
            .limit(pagination.limit)
        )
        return list((await self.session.execute(stmt)).unique().scalars().all()), total

    async def get_municipality(self, identifier: str | UUID) -> Municipality:
        """Look up a municipality by UUID, code or slug."""
        stmt = select(Municipality).options(
            selectinload(Municipality.province), selectinload(Municipality.district)
        )
        if isinstance(identifier, UUID):
            stmt = stmt.where(Municipality.id == identifier)
        else:
            lowered = str(identifier).lower()
            try:
                stmt = stmt.where(Municipality.id == UUID(str(identifier)))
            except ValueError:
                stmt = stmt.where(
                    or_(
                        func.lower(Municipality.slug) == lowered,
                        func.lower(Municipality.code) == lowered,
                        func.lower(Municipality.name) == lowered,
                    )
                )
        municipality = (await self.session.execute(stmt)).unique().scalars().first()
        if municipality is None:
            raise MunicipalityNotFoundError(details={"identifier": str(identifier)})
        return municipality

    async def resolve_municipality_id(self, name: str) -> UUID | None:
        """Best-effort municipality resolution by name/alias.

        Returns ``None`` when there is no confident match — the normalizer then
        leaves ``municipality_id`` NULL rather than guessing.
        """
        cleaned = " ".join(name.split()).lower()
        if not cleaned:
            return None
        stmt = select(Municipality.id).where(
            or_(
                func.lower(Municipality.name) == cleaned,
                func.lower(Municipality.slug) == cleaned.replace(" ", "-"),
                func.lower(Municipality.code) == cleaned,
            )
        )
        matches = list((await self.session.execute(stmt)).scalars().all())
        return matches[0] if len(matches) == 1 else None

    async def _count(self, stmt: Select[Any]) -> int:
        subquery = stmt.order_by(None).subquery()
        return int(
            (await self.session.execute(select(func.count()).select_from(subquery))).scalar_one()
        )
