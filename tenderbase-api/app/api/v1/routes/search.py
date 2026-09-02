"""Full-text search endpoint."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.api.dependencies import MetaDep, PaginationDep, SearchServiceDep
from app.api.v1.routes.tenders import TenderFilterDep
from app.schemas.common import ListResponse, PaginationMeta
from app.schemas.tender import SearchHit, SearchQuery

router = APIRouter(tags=["search"])


def search_query(
    filters: TenderFilterDep,
    q: Annotated[str, Query(min_length=2, max_length=300, description="Full-text query")],
    sort: Annotated[
        str, Query(description="relevance | published_at | closing_at (prefix '-' to descend)")
    ] = "relevance",
) -> SearchQuery:
    """Combine the shared tender filters with the required search term."""
    payload = filters.model_dump()
    payload.update(q=q, sort=sort)
    return SearchQuery(**payload)


@router.get(
    "/search",
    response_model=ListResponse[SearchHit],
    summary="Search procurement opportunities",
    description=(
        "Full-text search across title, reference number and description, with "
        "the same filter surface as `/tenders`.\n\n"
        "On PostgreSQL this uses full-text search with relevance ranking "
        "(`ts_rank`); each hit carries a `score` and a `snippet`. The response "
        "contract is engine-independent, so a dedicated search cluster can be "
        "introduced later without breaking clients.\n\n"
        "Example: `/api/v1/search?q=solar+installation&province=KwaZulu-Natal`."
    ),
)
async def search(
    service: SearchServiceDep,
    pagination: PaginationDep,
    meta: MetaDep,
    query: Annotated[SearchQuery, Depends(search_query)],
) -> ListResponse[SearchHit]:
    response = await service.search(query, pagination)

    hits: list[SearchHit] = []
    for result in response.results:
        hit = SearchHit.model_validate(result.opportunity)
        hit.score = result.score
        hit.snippet = result.snippet
        hits.append(hit)

    meta.extra = {
        "query": query.q,
        "search_backend": response.backend,
        "took_ms": response.took_ms,
    }
    return ListResponse[SearchHit](
        data=hits,
        pagination=PaginationMeta.build(
            page=pagination.page, page_size=pagination.page_size, total_items=response.total
        ),
        meta=meta,
    )
