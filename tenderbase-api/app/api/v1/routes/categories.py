"""Category endpoints."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query
from sqlalchemy import func, select

from app.api.dependencies import MetaDep, PaginationDep, SessionDep
from app.db.models.category import Category
from app.schemas.category import CategoryRead
from app.schemas.common import ListResponse, PaginationMeta

router = APIRouter(prefix="/categories", tags=["categories"])


@router.get(
    "",
    response_model=ListResponse[CategoryRead],
    summary="List procurement categories",
    description=(
        "The procurement category taxonomy. Category slugs can be passed to "
        "`/tenders?category=` and `/search?category=`. Categories are assigned "
        "to opportunities by deterministic keyword rules; AI classification is "
        "an optional later enrichment stage."
    ),
)
async def list_categories(
    session: SessionDep,
    pagination: PaginationDep,
    meta: MetaDep,
    taxonomy: Annotated[str | None, Query(description="Filter by taxonomy name")] = None,
    active: Annotated[bool | None, Query()] = None,
) -> ListResponse[CategoryRead]:
    stmt = select(Category)
    if taxonomy:
        stmt = stmt.where(Category.taxonomy == taxonomy)
    if active is not None:
        stmt = stmt.where(Category.active.is_(active))

    total = int(
        (
            await session.execute(select(func.count()).select_from(stmt.order_by(None).subquery()))
        ).scalar_one()
    )
    stmt = stmt.order_by(Category.name.asc()).offset(pagination.offset).limit(pagination.limit)
    categories = list((await session.execute(stmt)).unique().scalars().all())
    return ListResponse[CategoryRead](
        data=[CategoryRead.model_validate(item) for item in categories],
        pagination=PaginationMeta.build(
            page=pagination.page, page_size=pagination.page_size, total_items=total
        ),
        meta=meta,
    )
