"""Category and statistics schemas."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import Field

from app.schemas.common import TenderBaseModel


class CategoryRead(TenderBaseModel):
    id: UUID
    name: str
    slug: str
    description: str | None = None
    parent_id: UUID | None = None
    taxonomy: str = "tenderbase-core"
    active: bool = True


class CountByKey(TenderBaseModel):
    key: str
    label: str | None = None
    count: int


class StatisticsResponse(TenderBaseModel):
    """Aggregate platform statistics.

    Counts are computed from real ingested records only — the platform never
    reports fabricated coverage numbers.
    """

    generated_at: datetime
    total_opportunities: int
    open_opportunities: int
    closing_next_7_days: int
    total_documents: int
    documents_downloaded: int
    documents_with_text: int
    total_sources: int
    active_sources: int
    total_municipalities: int
    municipalities_with_sources: int = Field(
        description="Municipalities that have at least one configured source"
    )
    by_province: list[CountByKey] = Field(default_factory=list)
    by_procurement_type: list[CountByKey] = Field(default_factory=list)
    by_status: list[CountByKey] = Field(default_factory=list)
    by_source_health: list[CountByKey] = Field(default_factory=list)
    test_fixture_opportunities: int = Field(
        default=0, description="Records flagged as development fixtures (excluded by default)"
    )
