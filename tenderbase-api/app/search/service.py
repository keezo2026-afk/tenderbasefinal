"""Search service.

PostgreSQL is the initial search engine: full-text search over
``title``/``description`` with ``plainto_tsquery`` and ranking, plus trigram
similarity when ``pg_trgm`` is available.

The public contract is the :class:`SearchBackend` interface — swapping in
OpenSearch/Elasticsearch later requires no change to the API layer, because
routes depend on this interface rather than on SQL.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Select, func, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.opportunity import ProcurementOpportunity
from app.logging import get_logger
from app.utils.text import collapse_whitespace, truncate

logger = get_logger("tenderbase.search")

SNIPPET_LENGTH = 240
#: Columns participating in full-text search, with their weights.
FTS_LANGUAGE = "english"


@dataclass(slots=True)
class SearchResult:
    """One search hit: the ORM row plus relevance metadata."""

    opportunity: ProcurementOpportunity
    score: float | None = None
    snippet: str | None = None


@dataclass(slots=True)
class SearchResponse:
    """A page of search results."""

    results: list[SearchResult] = field(default_factory=list)
    total: int = 0
    backend: str = "postgres"
    took_ms: float | None = None


class SearchBackend(ABC):
    """Interface any search implementation must satisfy."""

    name: str = "abstract"

    @abstractmethod
    def apply_text_filter(self, stmt: Select[Any], query: str) -> tuple[Select[Any], Any]:
        """Apply the text predicate; return ``(statement, rank_expression)``."""

    @abstractmethod
    def snippet(self, opportunity: ProcurementOpportunity, query: str) -> str | None:
        """Produce a short highlight snippet for a hit."""


class PostgresSearchBackend(SearchBackend):
    """PostgreSQL full-text search with ranking."""

    name = "postgres"

    def _tsvector(self) -> Any:
        return func.to_tsvector(
            FTS_LANGUAGE,
            func.coalesce(ProcurementOpportunity.title, "")
            + " "
            + func.coalesce(ProcurementOpportunity.reference_number, "")
            + " "
            + func.coalesce(ProcurementOpportunity.description, ""),
        )

    def apply_text_filter(self, stmt: Select[Any], query: str) -> tuple[Select[Any], Any]:
        tsquery = func.plainto_tsquery(FTS_LANGUAGE, query)
        vector = self._tsvector()
        rank = func.ts_rank(vector, tsquery)
        return stmt.where(vector.op("@@")(tsquery)), rank

    def snippet(self, opportunity: ProcurementOpportunity, query: str) -> str | None:
        return _local_snippet(opportunity, query)


class SQLFallbackSearchBackend(SearchBackend):
    """Portable ILIKE-based search.

    Used for SQLite (tests/local development). It is intentionally simple —
    correctness over cleverness — and produces the same API contract.
    """

    name = "sql-fallback"

    def apply_text_filter(self, stmt: Select[Any], query: str) -> tuple[Select[Any], Any]:
        terms = [term for term in collapse_whitespace(query).split(" ") if len(term) > 1][:8]
        if not terms:
            return stmt, None
        clauses = []
        for term in terms:
            pattern = f"%{term}%"
            clauses.append(
                or_(
                    ProcurementOpportunity.title.ilike(pattern),
                    ProcurementOpportunity.description.ilike(pattern),
                    ProcurementOpportunity.reference_number.ilike(pattern),
                )
            )
        # All terms must appear somewhere (AND semantics), like plainto_tsquery.
        for clause in clauses:
            stmt = stmt.where(clause)
        return stmt, None

    def snippet(self, opportunity: ProcurementOpportunity, query: str) -> str | None:
        return _local_snippet(opportunity, query)


def _local_snippet(opportunity: ProcurementOpportunity, query: str) -> str | None:
    """Extract a text window around the first matching term."""
    body = collapse_whitespace(opportunity.description or opportunity.title or "")
    if not body:
        return None
    lowered = body.lower()
    for term in collapse_whitespace(query).lower().split(" "):
        if len(term) < 2:
            continue
        position = lowered.find(term)
        if position != -1:
            start = max(0, position - SNIPPET_LENGTH // 3)
            window = body[start : start + SNIPPET_LENGTH]
            return (
                ("…" if start > 0 else "")
                + window.strip()
                + ("…" if start + SNIPPET_LENGTH < len(body) else "")
            )
    return truncate(body, SNIPPET_LENGTH)


def get_search_backend(session: AsyncSession) -> SearchBackend:
    """Choose the backend appropriate for the connected database."""
    dialect = session.bind.dialect.name if session.bind is not None else ""
    if dialect == "postgresql":
        return PostgresSearchBackend()
    return SQLFallbackSearchBackend()
