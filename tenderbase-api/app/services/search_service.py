"""Search service facade.

Routes depend on this thin facade rather than on the query builder, so the
underlying engine (PostgreSQL today, a dedicated search cluster later) can be
replaced without touching the API contract.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.common import PaginationParams
from app.schemas.tender import SearchQuery
from app.search.service import SearchResponse, get_search_backend
from app.services.tender_service import TenderService


class SearchService:
    """Full-text search over procurement opportunities."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self._tenders = TenderService(session)

    async def search(self, query: SearchQuery, pagination: PaginationParams) -> SearchResponse:
        """Execute a search request and return ranked hits."""
        return await self._tenders.search(query, pagination)

    @property
    def backend_name(self) -> str:
        """Identifier of the active search backend (reported in response meta)."""
        return get_search_backend(self.session).name
